# Copyright © 2020 Collabora Ltd.
#
# SPDX-License-Identifier: MIT
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be included
# in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
# IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
# CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
# TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
# SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

import datetime
import hashlib
import io
import logging
import netrc
import os
import re
import shutil
import ssl
import subprocess
import tarfile
import tempfile
import typing
import urllib.error
import urllib.request
import zipfile
from contextlib import suppress


logger = logging.getLogger('runtimeutil')


class HashingWriter(io.BufferedIOBase):
    def __init__(self, name, mode):     # type: (str, str) -> None
        self._writer = open(name, mode)
        self._sha256 = hashlib.sha256()
        self.size = 0

    def write(self, blob):
        self.size += len(blob)
        self._sha256.update(blob)
        self._writer.write(blob)
        return len(blob)

    @property
    def sha256(self):   # type: () -> str
        return self._sha256.hexdigest()

    def __enter__(self):    # type: () -> HashingWriter
        return self

    def __exit__(self, *rest):
        return self._writer.__exit__(*rest)


def netrc_password_manager(
    path: typing.Union[os.PathLike, str],
) -> urllib.request.HTTPPasswordMgr:
    """
    Load a file in .netrc format from the given file.
    Return a HTTPPasswordMgr that will present passwords from that file.
    """
    loader = netrc.netrc(str(path))

    password_manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()

    for host in loader.hosts:
        triple = loader.authenticators(host)

        if triple is None:
            continue

        login, _, password = triple
        password_manager.add_password(
            None,       # type: ignore
            'https://' + host,
            login,
            password,
        )

    return password_manager


def verbose_urlopen(
    url: str,
    *,
    opener: typing.Optional[urllib.request.OpenerDirector] = None,
) -> typing.BinaryIO:
    logger.info('Requesting <%s>...', url)
    try:
        if opener is None:
            return urllib.request.urlopen(url)
        else:
            return opener.open(url)
    except urllib.error.URLError:
        logger.error('Error opening <%s>:', url)
        raise


def slugify(s: str) -> str:
    """Convert s into a (possibly empty) string suitable for use in
    filenames, URLs etc.
    """
    ret = []

    for c in s:
        if ord(c) < 128 and c.isalnum():
            ret.append(c.lower())
        else:
            ret.append('-')

    return re.sub(r'-+', '-', ''.join(ret).strip('-'))


class SteamClient:
    def __init__(
        self,
        *,
        uri: str = 'https://steamcdn-a.akamaihd.net/client',
        manifest: str = 'steam_client_ubuntu12'
    ):
        self.uri = uri
        self.manifest = manifest
        self.version = None         # type: typing.Optional[str]
        self.datetime = None        # type: typing.Optional[datetime.datetime]
        self.manifest_content = {
        }       # type: typing.Dict[str, typing.Dict[str, typing.Any]]
        self.have_scout_manifest = False
        self.scout_version = None               # type: typing.Optional[str]
        self.scout_version_marker = None        # type: typing.Optional[str]

    def download_manifest(
        self,
        datadir: str
    ) -> None:

        os.makedirs(datadir, exist_ok=True)

        logger.info(
            'Downloading Steam client manifest from %s/%s...',
            self.uri, self.manifest,
        )
        with verbose_urlopen(
            '{}/{}'.format(self.uri, self.manifest)
        ) as http_reader:
            with open(
                os.path.join(datadir, 'manifest.vdf'), 'wb'
            ) as writer:
                # mypy can't figure out that this is
                # copyfileobj(BinaryIO -> BinaryIO)
                shutil.copyfileobj(http_reader, writer)     # type: ignore

        self.load_manifest(datadir)

    def download_client(
        self,
        datadir: str,
        strict: bool = True,
    ) -> None:
        names = []      # type: typing.List[str]

        with tempfile.TemporaryDirectory() as tempdir:
            for name, value in sorted(
                self.manifest_content['ubuntu12'].items(),
                key=lambda i: i[0]
            ):
                if name == 'version':
                    continue

                basename = value['file']
                assert '/' not in basename, basename
                names.append(basename)
                with verbose_urlopen(
                    '{}/{}'.format(self.uri, basename)
                ) as downloader:
                    with HashingWriter(
                        os.path.join(tempdir, basename),
                        'wb'
                    ) as hasher:
                        shutil.copyfileobj(downloader, hasher)
                        if hasher.sha256 != value['sha2']:
                            logger.warning(
                                'Unexpected hash for %s/%s\n'
                                '  Expected: %s\n'
                                '  Got     : %s\n',
                                self.uri, basename,
                                value['sha2'], hasher.sha256)

                            if strict:
                                raise ValueError('sha256 mismatch')

                        if hasher.size != int(value['size']):
                            logger.warning(
                                'Unexpected size for %s/%s\n'
                                '  Expected: %s\n'
                                '  Got     : %s\n',
                                self.uri, basename,
                                value['size'], hasher.size)

                            if strict:
                                raise ValueError(
                                    'size mismatch (sha256 collision?!)'
                                )

            for basename in names:
                with zipfile.ZipFile(
                    os.path.join(tempdir, basename), 'r'
                ) as unzip:
                    for part in unzip.infolist():
                        part.filename = part.filename.replace('\\', '/')
                        unzip.extract(part, path=datadir)

    def download_scout(
        self,
        datadir: str,
        strict: bool = False,
    ) -> None:
        self.download_runtime(1, datadir=datadir, strict=strict)

    def download_runtime(
        self,
        major_version: typing.Union[int, str],
        datadir: str,
        strict: bool = False,
    ) -> bool:
        zip_name = ''
        zip_part_names: typing.List[str] = []

        suite = RuntimeArchive.CODENAMED_SUITE_VERSIONS.get(
            str(major_version),
            f'steamrt{major_version}',
        )

        with tempfile.TemporaryDirectory() as tempdir:
            for name, value in sorted(
                self.manifest_content['ubuntu12'].items(),
                key=lambda i: i[0]
            ):
                is_whole = (name == f'runtime_{suite}_ubuntu12')
                is_part = False

                if suite == 'scout':
                    is_part = (
                        name.startswith('runtime_part')
                        and name.endswith('_ubuntu12')
                    )

                if is_whole or is_part:
                    basename = value['file']
                    assert '/' not in basename, basename

                    if is_whole:
                        assert not zip_name, (zip_name, basename)
                        zip_name = basename
                    else:
                        zip_part_names.append(basename)

                    with verbose_urlopen(
                        '{}/{}'.format(self.uri, basename)
                    ) as downloader:
                        with HashingWriter(
                            os.path.join(tempdir, basename),
                            'wb'
                        ) as hasher:
                            shutil.copyfileobj(downloader, hasher)
                            if hasher.sha256 != value['sha2']:
                                logger.warning(
                                    'Unexpected hash for %s/%s\n'
                                    '  Expected: %s\n'
                                    '  Got     : %s\n',
                                    self.uri, basename,
                                    value['sha2'], hasher.sha256)

                                if strict:
                                    raise ValueError('sha256 mismatch')

                            if hasher.size != int(value['size']):
                                logger.warning(
                                    'Unexpected size for %s/%s\n'
                                    '  Expected: %s\n'
                                    '  Got     : %s\n',
                                    self.uri, basename,
                                    value['size'], hasher.size)

                                if strict:
                                    raise ValueError('sha256 mismatch')

            tar_name = {
                'scout': 'steam-runtime.tar.xz',
                'sniper': 'SteamLinuxRuntime_sniper.tar.xz',
            }.get(suite, f'SteamLinuxRuntime_{major_version}.tar.xz')

            look_for = {
                'scout': [f'ubuntu12_32/{tar_name}'],
                'sniper': [
                    f'ubuntu12_64/{tar_name}',
                    f'ubuntu12_64/steam-runtime-{suite}.tar.xz',
                ],
            }.get(suite, [f'ubuntu12_64/{tar_name}'])

            found_parts = False

            with open(
                os.path.join(datadir, tar_name), 'wb'
            ) as tar_writer:
                if zip_name:
                    with zipfile.ZipFile(
                        os.path.join(tempdir, zip_name), 'r'
                    ) as unzip:
                        for part in unzip.infolist():
                            if part.filename.replace('\\', '/') in look_for:
                                with unzip.open(part) as part_reader:
                                    shutil.copyfileobj(
                                        part_reader,
                                        tar_writer
                                    )       # type: ignore
                                    return True

                    raise AssertionError(f'{suite} not found in {zip_name}')

                # Fallback: scout used to be shipped split into parts
                for basename in zip_part_names:
                    with zipfile.ZipFile(
                        os.path.join(tempdir, basename), 'r'
                    ) as unzip:
                        for part in unzip.infolist():
                            if '.tar.xz.part' in part.filename:
                                with unzip.open(part) as part_reader:
                                    # mypy can't figure out that this
                                    # is copyfileobj(BinaryIO -> BinaryIO)
                                    shutil.copyfileobj(
                                        part_reader,
                                        tar_writer
                                    )       # type: ignore
                                    found_parts = True

            return found_parts

    def load_manifest(
        self,
        datadir: str,
    ) -> None:
        import vdf

        with open(
            os.path.join(datadir, 'manifest.vdf'), 'r'
        ) as text_reader:
            self.manifest_content = vdf.load(text_reader)

        self.version = str(
            self.manifest_content['ubuntu12']['version']
        )

        logger.info(
            'Steam client build: %s', self.version,
        )

        try:
            timestamp = int(self.version)
        except ValueError:
            self.datetime = None
        else:
            self.datetime = datetime.datetime.fromtimestamp(
                timestamp,
                datetime.timezone.utc,
            )
            logger.info(
                'Steam client build date (probably) %s',
                self.datetime.strftime('%Y-%m-%d %H:%M:%S%z'),
            )

    def extract_scout(
        self,
        runtimedir: str,
        destdir: str,
        extract_manifest: bool = False,
    ) -> None:

        with suppress(FileNotFoundError):
            shutil.rmtree(os.path.join(runtimedir, 'steam-runtime'))

        with suppress(FileNotFoundError):
            shutil.rmtree(os.path.join(destdir, 'steam-runtime'))

        with suppress(FileExistsError):
            os.mkdir(os.path.join(runtimedir, 'steam-runtime'))

        with suppress(FileExistsError):
            os.mkdir(os.path.join(destdir, 'steam-runtime'))

        with tarfile.open(
            os.path.join(runtimedir, 'steam-runtime.tar.xz'), mode='r|xz',
        ) as tar_reader:
            for info in tar_reader:
                if not info.isfile():
                    continue

                if info.name in (
                    'steam-runtime/version.txt',
                ):
                    tar_reader.extract(info, path=destdir)
                    continue

                if extract_manifest and info.name in (
                    'steam-runtime/built-using.txt',
                    'steam-runtime/manifest.deb822.gz',
                    'steam-runtime/manifest.txt',
                ):
                    tar_reader.extract(info, path=destdir)

                    if info.name == 'steam-runtime/manifest.deb822.gz':
                        self.have_scout_manifest = True

                    continue

        with suppress(
            FileNotFoundError
        ), open(
            os.path.join(
                destdir, 'steam-runtime', 'version.txt'),
            'r'
        ) as text_reader:
            marker = text_reader.read().strip()

        self.scout_version_marker = marker
        version_bits = marker.split('_')

        if len(version_bits) != 2 or version_bits[0] != 'steam-runtime':
            logger.warning(
                'Unexpected format for runtime version: %s', marker,
            )
        else:
            self.scout_version = version_bits[-1]
            logger.info('scout runtime version %s', self.scout_version)

    def get_container_runtime_version(
        self,
        major_version: typing.Union[int, str],
        runtimedir: str,
    ) -> typing.Optional[str]:
        suite = RuntimeArchive.CODENAMED_SUITE_VERSIONS.get(
            str(major_version),
            f'steamrt{major_version}',
        )

        if suite == 'sniper':
            top_dir = f'SteamLinuxRuntime_{suite}'
        else:
            top_dir = f'SteamLinuxRuntime_{major_version}'

        with suppress(FileNotFoundError):
            shutil.rmtree(os.path.join(runtimedir, top_dir))

        with suppress(FileExistsError):
            os.mkdir(os.path.join(runtimedir, top_dir))

        candidates = (
            top_dir + '.tar.xz',
            f'steam-runtime-{suite}.tar.xz',
        )

        for candidate in candidates:
            if os.path.exists(os.path.join(runtimedir, candidate)):
                tarball = candidate
                break

            candidate = os.path.join('ubuntu12_64', candidate)

            if os.path.exists(os.path.join(runtimedir, candidate)):
                tarball = candidate
                break
        else:
            logger.warning('One of %r not found', candidates)
            return None

        text = ''

        # For whatever reason, the initial steam-runtime-sniper.tar.xz
        # was actually bz2-compressed, so let tarfile auto-detect
        with tarfile.open(
            os.path.join(runtimedir, tarball),
            mode='r:*',
        ) as tar_reader:
            for info in tar_reader:
                if not info.isfile():
                    continue

                if info.name in (
                    f'{top_dir}/VERSIONS.txt',
                    f'steam-runtime-{suite}/VERSIONS.txt',
                ):
                    extractor = tar_reader.extractfile(info)
                    assert extractor is not None

                    with extractor as member_reader:
                        text = member_reader.read().decode('utf-8')

        for line in text.splitlines():
            if line.startswith('depot\t'):
                return line.split('\t')[1]
            elif line.startswith(f'{suite}\t'):
                return line.split('\t')[1]

        return None


class QuietError(Exception):
    """
    An error that usually doesn't provoke a traceback.
    """


def build_opener(
    *,
    handlers: typing.Iterable[urllib.request.BaseHandler] = [],
    password_manager: typing.Optional[urllib.request.HTTPPasswordMgr] = None,
    ssl_context: typing.Optional[ssl.SSLContext] = None
) -> urllib.request.OpenerDirector:
    hs = list(handlers)

    if ssl_context is not None:
        hs.append(urllib.request.HTTPSHandler(context=ssl_context))

    if password_manager is not None:
        hs.append(urllib.request.HTTPBasicAuthHandler(password_manager))

    return urllib.request.build_opener(*hs)


class PinnedRuntimeVersion:
    def __init__(self, v: str, archive: 'RuntimeArchive') -> None:
        if not v or not v[0].isdigit():
            raise ValueError(
                'Runtime version {!r} does not start with a digit'.format(v)
            )

        for c in v:
            if c != '.' and not c.isdigit():
                raise ValueError(
                    'Runtime version {!r} contains non-dot, non-digit'.format(
                        v
                    )
                )

        self.version = v
        self.archive = archive

    def __eq__(self, other):
        return (
            isinstance(other, PinnedRuntimeVersion)
            and self.version == other.version
            and self.archive is other.archive
        )

    def __lt__(self, other):
        if not isinstance(other, PinnedRuntimeVersion):
            raise TypeError(
                'Cannot compare {!r} with {!r}'.format(self, other)
            )

        if self.archive is not other.archive:
            raise TypeError(
                'Cannot compare {!r} with {!r}'.format(self, other)
            )

        return self.version < other.version

    def __le__(self, other):
        if not isinstance(other, PinnedRuntimeVersion):
            raise TypeError(
                'Cannot compare {!r} with {!r}'.format(self, other)
            )

        return self == other or self < other

    def __gt__(self, other):
        if not isinstance(other, PinnedRuntimeVersion):
            raise TypeError(
                'Cannot compare {!r} with {!r}'.format(self, other)
            )

        return other < self

    def __ge__(self, other):
        if not isinstance(other, PinnedRuntimeVersion):
            raise TypeError(
                'Cannot compare {!r} with {!r}'.format(self, other)
            )

        return self == other or other < self

    def __hash__(self):
        return hash(self.version)

    def __str__(self) -> str:
        return self.version

    def __repr__(self) -> str:
        return '<PinnedRuntimeVersion {!r} in {!r}>'.format(
            self.version,
            self.archive,
        )

    def get_uri(self, filename: str) -> str:
        return self.archive.get_uri(self.version, filename)

    def open(self, filename: str) -> typing.BinaryIO:
        return self.archive.open(self.version, filename)

    def fetch(
        self,
        filename: str,
        destdir: str,
        *,
        log_level: typing.Optional[int] = None,
        must_exist: bool = True
    ) -> None:
        self.archive.fetch(
            self.version, filename, destdir,
            log_level=log_level, must_exist=must_exist,
        )


class RuntimeArchive:
    DEFAULT_IMAGES_URI = (
        'https://repo.steampowered.com/IMAGES_DIR/snapshots'
    )
    DEFAULT_SSH_HOST = None     # type: typing.Optional[str]
    DEFAULT_SSH_ROOT = None     # type: typing.Optional[str]

    # Older branches of the Steam Runtime have a Team Fortress 2 character
    # class as a codename. Newer branches are just called 'steamrt5' and
    # so on.
    SUITE_CODENAMES = {
        'scout': '1',
        'soldier': '2',
        'sniper': '3',
        'medic': '4',
    }
    CODENAMED_SUITE_VERSIONS = {
        '1': 'scout',
        '2': 'soldier',
        '3': 'sniper',
        '4': 'medic',
    }

    def __init__(
        self,
        suite: str,
        *,
        images_uri: typing.Optional[str] = None,
        opener: typing.Optional[urllib.request.OpenerDirector] = None,
        password_manager: typing.Optional[
            urllib.request.HTTPPasswordMgr
        ] = None,
        ssh_host: typing.Optional[str] = None,
        ssh_path: typing.Optional[str] = None,
        ssh_root: typing.Optional[str] = None,
        ssh_user: typing.Optional[str] = None,
        ssl_context: typing.Optional[ssl.SSLContext] = None
    ) -> None:
        cls = self.__class__

        self.suite = suite

        if ssh_host is None:
            ssh_host = cls.DEFAULT_SSH_HOST

        if images_uri is None:
            if suite in cls.SUITE_CODENAMES:
                topdir = f'steamrt-{suite}'
                images_dir = f'steamrt-images-{suite}'
            else:
                topdir = images_dir = suite

            images_uri = (
                cls.DEFAULT_IMAGES_URI
            ).replace(
                'SUITE', suite
            ).replace(
                'TOPDIR', topdir
            ).replace(
                'IMAGES_DIR', images_dir
            )

        self.images_uri = images_uri

        if ssl_context is None:
            ssl_context = cls.create_ssl_context()

        if opener is None:
            opener = build_opener(
                password_manager=password_manager,
                ssl_context=ssl_context,
            )

        self.opener = opener

        if ssh_host is not None:
            if ssh_root is None:
                ssh_root = cls.DEFAULT_SSH_ROOT

            if ssh_root is None:
                ssh_root = f'/srv/{ssh_host}/www'

            if ssh_path is None:
                if suite in cls.SUITE_CODENAMES:
                    ssh_path = f'steamrt-{suite}'
                else:
                    ssh_path = suite

            if not ssh_path.startswith('/'):
                ssh_path = f'{ssh_root}/{ssh_path}'

            if ssh_user is None:
                self.ssh_target = ssh_host      # type: typing.Optional[str]
            else:
                self.ssh_target = ssh_user + '@' + ssh_host
        else:
            self.ssh_target = None

        self.ssh_path = ssh_path

    @classmethod
    def create_ssl_context(cls) -> ssl.SSLContext:
        return ssl.create_default_context()

    def __repr__(self) -> str:
        return '<RuntimeArchive {!r}>'.format(self.suite)

    def get_uri(
        self,
        version: str,
        filename: str,
    ) -> str:
        return '{}/{}/{}'.format(self.images_uri, version, filename)

    def open(
        self,
        version: str,
        filename: str,
        *,
        log_level: int = logging.ERROR,
    ) -> typing.BinaryIO:
        """
        Open and stream the given file in the given version of this
        runtime. Unlike fetch(), this always uses http or https.

        If we cannot open it, log a message at level log_level and reraise
        the exception.
        """
        uri = self.get_uri(version, filename)

        logger.info('Requesting <%s>...', uri)
        try:
            return self.opener.open(uri)
        except urllib.error.URLError:
            if log_level > logging.NOTSET:
                logger.log(log_level, 'Error opening <%s>:', uri)
            raise

    def pin_version(
        self,
        version: str,
        *,
        log_level: int = logging.ERROR,
    ) -> PinnedRuntimeVersion:
        """
        Get the "pinned" version corresponding to the given
        symbolic version.
        If it's a version number rather than a symbolic version,
        just return it as a PinnedRuntimeVersion.
        """
        with self.open(
            version, 'VERSION.txt', log_level=log_level
        ) as http_reader:
            return PinnedRuntimeVersion(
                http_reader.read().decode('ascii').strip(),
                self,
            )

    def fetch(
        self,
        version: str,
        filename: str,
        destdir: str,
        *,
        log_level: typing.Optional[int] = None,
        must_exist: bool = True,
        rsync: bool = True,
    ) -> None:
        """
        Download the given file from the given version of this runtime.
        Write it to a file of the same basename in destdir.

        Use rsync for an incremental transfer if possible, unless @rsync
        is false.
        """
        ssh_target = self.ssh_target
        ssh_path = self.ssh_path

        if log_level is None:
            if must_exist:
                log_level = logging.ERROR
            else:
                log_level = logging.INFO

        if (
            ssh_target is not None
            and ssh_path is not None
            and must_exist
            and rsync
        ):
            path = f'{ssh_path}/{version}/{filename}'
            logger.info('Downloading %r...', path)
            subprocess.run([
                'rsync',
                '--archive',
                '--partial',
                '--progress',
                ssh_target + ':' + path,
                os.path.join(destdir, filename),
            ], check=True)
        else:
            try:
                with self.open(
                    version, filename,
                    log_level=log_level,
                ) as response, open(
                    os.path.join(destdir, filename), 'wb',
                ) as writer:
                    # mypy can't figure out that this is
                    # copyfileobj(BinaryIO -> BinaryIO)
                    shutil.copyfileobj(response, writer)    # type: ignore
            except urllib.error.URLError:
                with suppress(FileNotFoundError):
                    os.remove(os.path.join(destdir, filename))

                if must_exist:
                    raise
