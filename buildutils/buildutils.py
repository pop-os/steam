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
import os
import shutil
import tarfile
import tempfile
import typing
import urllib.error
import urllib.request
import zipfile
from contextlib import suppress

import vdf


logger = logging.getLogger('buildutils-lib')


class HashingWriter(io.BufferedIOBase):
    def __init__(self, name, mode):     # type: (str, str) -> None
        self._writer = open(name, mode)
        self._sha256 = hashlib.sha256()
        self.size = 0

    def write(self, blob):
        blob = bytes(blob)
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


def verbose_urlopen(
    url     # type: str
):
    # type: (...) -> typing.BinaryIO

    logger.info('Requesting <%s>...', url)
    try:
        return urllib.request.urlopen(url)
    except urllib.error.URLError:
        logger.error('Error opening <%s>:', url)
        raise


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
        self.have_runtime_manifest = False
        self.runtime_version = None             # type: typing.Optional[str]
        self.runtime_version_marker = None      # type: typing.Optional[str]

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
                shutil.copyfileobj(http_reader, writer)

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
                                raise ValueError('sha256 mismatch')

            for basename in names:
                with zipfile.ZipFile(
                    os.path.join(tempdir, basename), 'r'
                ) as unzip:
                    for part in unzip.infolist():
                        part.filename = part.filename.replace('\\', '/')
                        unzip.extract(part, path=datadir)

    def download_runtime(
        self,
        datadir: str,
        strict: bool = False,
    ) -> None:
        names = []      # type: typing.List[str]

        with tempfile.TemporaryDirectory() as tempdir:
            for name, value in sorted(
                self.manifest_content['ubuntu12'].items(),
                key=lambda i: i[0]
            ):
                if (name.startswith('runtime_part')
                        and name.endswith('_ubuntu12')):
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
                                    raise ValueError('sha256 mismatch')

            with open(
                os.path.join(datadir, 'steam-runtime.tar.xz'), 'wb'
            ) as tar_writer:
                for basename in names:
                    with zipfile.ZipFile(
                        os.path.join(tempdir, basename), 'r'
                    ) as unzip:
                        for part in unzip.infolist():
                            if '.tar.xz.part' in part.filename:
                                with unzip.open(part) as part_reader:
                                    shutil.copyfileobj(
                                        part_reader, tar_writer)

    def load_manifest(
        self,
        datadir: str,
    ) -> None:
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

    def extract_runtime(
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
                        self.have_runtime_manifest = True

                    continue

        with suppress(
            FileNotFoundError
        ), open(
            os.path.join(
                destdir, 'steam-runtime', 'version.txt'),
            'r'
        ) as text_reader:
            marker = text_reader.read().strip()

        self.runtime_version_marker = marker
        version_bits = marker.split('_')

        if len(version_bits) != 2 or version_bits[0] != 'steam-runtime':
            logger.warning(
                'Unexpected format for runtime version: %s', marker,
            )
        else:
            self.runtime_version = version_bits[-1]
            logger.info('Runtime version %s', self.runtime_version)
