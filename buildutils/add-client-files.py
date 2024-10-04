#!/usr/bin/env python3

# Copyright © 2019-2020 Collabora Ltd.
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

"""
Prepare external resources needed to build the Steam launcher .deb files.
"""

import argparse
import fnmatch
import json
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Sequence,
)

from buildutils import SteamClient

logger = logging.getLogger('add-client-files')


BOOTSTRAP_RUNTIME_AMD64_BIN = (
    'steam-runtime-check-requirements',     # run by steam.sh
    'steam-runtime-identify-library-abi',   # run by setup.sh
    'steam-runtime-launch-client',          # run by s-r-check-requirements
    'srt-logger',                           # (symlink) run by steam.sh
)
BOOTSTRAP_RUNTIME_AMD64_SONAMES = (
    'libcap.so.2',                          # dependency of srt-bwrap
    'libelf.so.1',                          # dependency of s-r-id-lib-abi
    'libffi.so.6',                          # dependency of GObject
    'libgio-2.0.so.0',
    'libglib-2.0.so.0',
    'libgmodule-2.0.so.0',                  # dependency of Gio
    'libgobject-2.0.so.0',
    'libpcre.so.3',                         # dependency of GLib until #112
    'libselinux.so.1',                      # dependency of Gio until #112
    'libz.so.1',                            # dependency of Gio
)
BOOTSTRAP_RUNTIME_AMD64_PATTERNS = (
    'libelf-*.so',
)
BOOTSTRAP_RUNTIME_I386_SONAMES = (
    'libX11-xcb.so.1',
    'libX11.so.6',
    'libXau.so.6',
    'libXdamage.so.1',
    'libXdmcp.so.6',
    'libXext.so.6',
    'libXfixes.so.3',
    'libXxf86vm.so.1',
    'libcom_err.so.2',
    'libcurl-gnutls.so.4',
    'libexpat.so.1',
    'libffi.so.6',
    'libgcc_s.so.1',
    'libgcrypt.so.11',
    'libgmp.so.10',
    'libgnutls.so.30',
    'libgssapi_krb5.so.2',
    'libhogweed.so.4',
    'libidn.so.11',
    'libk5crypto.so.3',
    'libkeyutils.so.1',
    'libkrb5.so.3',
    'libkrb5support.so.0',
    'libnettle.so.6',
    'libp11-kit.so.0',
    'librtmp.so.0',
    'libstdc++.so.6',
    'libtasn1.so.6',
    'libtinfo.so.5',
    'libxcb-dri2.so.0',
    'libxcb-dri3.so.0',
    'libxcb-glx.so.0',
    'libxcb-present.so.0',
    'libxcb-sync.so.1',
    'libxcb.so.1',
    'libz.so.1',
)
BOOTSTRAP_RUNTIME_LIBEXEC_SRT = (
    'srt-bwrap',                            # run by s-r-check-requirements
    'srt-logger',                           # run by bin_steam.sh, steam.sh
    'logger-0.bash',                        # run by bin_steam.sh, steam.sh
)


class InvocationError(Exception):
    pass


class Main:
    def __init__(
        self,
        client_manifest: str,
        client_overlay: str,
        client_uri: str,
        destination: str,
        runtime_snapshots_uri: str,
        beta_universe: bool = False,
        client_dir: Optional[str] = None,
        client_tarball_uri: Optional[str] = None,
        credential_envs: Sequence[str] = (),
        runtime_version: str = '',
        **kwargs: Dict[str, Any],
    ) -> None:
        openers: List[urllib.request.BaseHandler] = []

        if credential_envs:
            password_manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()

            for item in credential_envs:
                server, credential_env = item.split('=', 1)
                username, password = os.environ[credential_env].split(':', 1)

                password_manager.add_password(
                    # We ignore the realm. The type annotations say None
                    # isn't allowed here, but the documentation says it is!
                    None,       # type: ignore
                    f'https://{server}/',
                    username,
                    password,
                )

            openers.append(
                urllib.request.HTTPBasicAuthHandler(password_manager)
            )

        self.opener = urllib.request.build_opener(*openers)

        self.client_dir = None
        self.client_tarball_uri = client_tarball_uri
        self.client_manifest = client_manifest
        self.client_overlay = client_overlay
        self.client_uri = client_uri
        self.client_version = None          # type: Optional[str]
        self.destination = destination
        self.resolved_runtime = None        # type: Optional[str]
        self.runtime_snapshots_uri = runtime_snapshots_uri
        self.runtime_version = runtime_version

        if beta_universe:
            self.package = 'steambeta'
        else:
            self.package = 'steam'

        if 'SOURCE_DATE_EPOCH' in os.environ:
            self.reference_timestamp = int(os.environ['SOURCE_DATE_EPOCH'])
        else:
            self.reference_timestamp = -1

    def fetch(
        self,
        uri: str,
        output: str,
    ) -> None:
        with self.opener.open(uri) as response:
            with open(output, 'wb') as writer:
                shutil.copyfileobj(response, writer)

    def get_runtime_uri(
        self,
        filename: str,
        version: str,
    ) -> str:
        return f'{self.runtime_snapshots_uri}/{version}/{filename}'

    def download_client(
        self,
        tmpdir: str,
    ) -> None:
        os.makedirs(os.path.join(tmpdir, 'client'), exist_ok=True)

        if self.client_tarball_uri is not None:
            self.fetch(
                self.client_tarball_uri,
                os.path.join(tmpdir, 'client.tar.gz'),
            )

            subprocess.run(
                [
                    'tar',
                    '--strip-components=1',
                    '-C', os.path.join(tmpdir, 'client'),
                    '-xvf', os.path.join(tmpdir, 'client.tar.gz'),
                ],
                check=True,
            )
        else:
            client = SteamClient(
                manifest=self.client_manifest,
                uri=self.client_uri,
            )

            client.download_manifest(datadir=tmpdir)
            client.download_client(
                datadir=os.path.join(tmpdir, 'client'),
                strict=True,
            )
            runtimedir = os.path.join(tmpdir, 'client', 'ubuntu12_32')
            client.download_scout(datadir=runtimedir, strict=True)
            client.extract_scout(runtimedir=runtimedir, destdir=tmpdir)

            self.client_version = client.version
            self.resolved_runtime = client.scout_version

        assert os.path.exists(
            os.path.join(tmpdir, 'client', 'steam.sh')
        )
        assert os.path.exists(
            os.path.join(tmpdir, 'client', 'ubuntu12_32', 'steam')
        )

    def ensure_scout_tarball(
        self,
        tmpdir: str,
    ) -> None:
        """
        Download a pre-prepared LD_LIBRARY_PATH Steam Runtime from a
        previous scout build.
        """
        path = os.path.join(
            tmpdir, 'client', 'ubuntu12_32', 'steam-runtime.tar.xz',
        )

        if not self.runtime_version:
            if not os.path.exists(path):
                raise InvocationError(
                    '--runtime-version must be specified if CLIENT_VERSION '
                    'does not contain a Steam Runtime tarball'
                )

            return

        uri = self.get_runtime_uri(
            filename='VERSION.txt',
            version=self.runtime_version,
        )
        logger.info('Requesting <%s>...', uri)

        with self.opener.open(uri) as response:
            resolved_runtime = response.read().strip().decode('utf-8')
            self.resolved_runtime = resolved_runtime

        logger.info(
            'Downloading steam-runtime build %s',
            resolved_runtime,
        )

        path = os.path.join(tmpdir, 'client', 'ubuntu12_32')

        os.makedirs(path, exist_ok=True)

        for filename in os.listdir(path):
            if (
                filename == 'steam-runtime.tar.xz'
                or filename.startswith('steam-runtime.tar.xz.part')
            ):
                os.remove(os.path.join(path, filename))

        filename = 'steam-runtime.tar.xz'

        self.fetch(
            self.get_runtime_uri(
                filename=filename,
                version=resolved_runtime,
            ),
            os.path.join(path, filename),
        )

        # The build script expects this to be split into parts.
        # Who are we to disagree? But for simplicity, we just use a
        # single part.
        os.link(
            os.path.join(path, filename),
            os.path.join(path, filename + '.part0'),
        )

    def install(
        self,
        src: str,
        dest: str,
        executable: bool = False,
    ) -> None:
        if executable:
            mode = '755'
        else:
            mode = '644'

        subprocess.run([
            'install', '-p', '-m', mode, src, dest,
        ], check=True)

    def install_search(
        self,
        dirs: Sequence[str],
        rel: str,
        dest: str,
        executable: bool = False,
    ) -> None:
        for d in dirs:
            if not d:
                continue

            src = os.path.join(d, rel)

            if os.path.exists(src):
                self.install(src, dest, executable=executable)
                return

        raise RuntimeError(f'{rel} not found in {dirs}')

    def _normalize_tar_entry(
        self,
        entry: tarfile.TarInfo,
    ) -> tarfile.TarInfo:
        entry.uid = 65534
        entry.gid = 65534

        if (
            self.reference_timestamp != -1
            and entry.mtime > self.reference_timestamp
        ):
            entry.mtime = self.reference_timestamp

        entry.uname = 'nobody'
        entry.gname = 'nogroup'

        return entry

    def want_file_from_runtime(
        self,
        path_parts: List[str],
    ) -> bool:
        if path_parts[0] in (
            'COPYING',
            'README.txt',
            'built-using.txt',
            'common-licenses',
            'manifest.deb822.gz',
            'manifest.txt',
            'run.sh',
            'scripts',
            'setup.sh',
            'version.txt',
        ):
            logger.info('[x] %s: metadata/scripts', '/'.join(path_parts))
            return True

        if path_parts[0] == 'usr':
            path_parts = path_parts[1:]

        if path_parts[:-1] in (
            ['lib', 'i386-linux-gnu'],
            ['lib', 'i386-linux-gnu', 'steam-runtime-tools-0'],
        ):
            for soname in BOOTSTRAP_RUNTIME_I386_SONAMES:
                if (
                    path_parts[-1] == soname
                    or path_parts[-1].startswith(soname + '.')
                ):
                    logger.info('[x] %s: i386 lib', '/'.join(path_parts))
                    return True
        elif path_parts[:-1] in (
            ['lib', 'x86_64-linux-gnu'],
            ['lib', 'x86_64-linux-gnu', 'steam-runtime-tools-0'],
        ):
            for pattern in BOOTSTRAP_RUNTIME_AMD64_PATTERNS:
                if fnmatch.fnmatch(path_parts[-1], pattern):
                    logger.info('[x] %s: amd64 lib', '/'.join(path_parts))
                    return True

            for soname in BOOTSTRAP_RUNTIME_AMD64_SONAMES:
                if (
                    path_parts[-1] == soname
                    or path_parts[-1].startswith(soname + '.')
                ):
                    logger.info('[x] %s: amd64 lib', '/'.join(path_parts))
                    return True
        elif path_parts[:-1] == ['amd64', 'usr', 'bin']:
            if path_parts[-1] in BOOTSTRAP_RUNTIME_AMD64_BIN:
                logger.info('[x] %s: amd64 bin', '/'.join(path_parts))
                return True
        elif (
            path_parts[:-1] == ['libexec', 'steam-runtime-tools-0']
        ):
            if path_parts[-1] in BOOTSTRAP_RUNTIME_LIBEXEC_SRT:
                logger.info('[x] %s: pkglibexec', '/'.join(path_parts))
                return True
        elif path_parts in (
            ['amd64', 'lib'],
            ['amd64', 'usr', 'lib'],
            ['amd64', 'usr', 'libexec'],
            ['amd64', 'usr', 'share'],
        ):
            logger.info('[x] %s: amd64 dependency', '/'.join(path_parts))
            return True

        logger.info('[ ] %s', '/'.join(path_parts))
        return False

    def build_bootstrap(
        self,
        client_dir: str,
        tmpdir: str,
    ) -> None:
        os.makedirs(
            os.path.join(tmpdir, 'bootstrap', 'clientui', 'fonts'),
            exist_ok=True,
            mode=0o755,
        )
        os.makedirs(
            os.path.join(tmpdir, 'bootstrap', 'linux32'),
            exist_ok=True,
            mode=0o755,
        )
        os.makedirs(
            os.path.join(tmpdir, 'bootstrap', 'ubuntu12_32'),
            exist_ok=True,
            mode=0o755,
        )

        self.install_search(
            (self.client_overlay, client_dir),
            'linux32/steamerrorreporter',
            os.path.join(tmpdir, 'bootstrap', 'linux32', ''),
            executable=True,
        )
        self.install_search(
            (self.client_overlay, client_dir),
            'steam.sh',
            os.path.join(tmpdir, 'bootstrap', ''),
            executable=True,
        )
        self.install_search(
            (self.client_overlay, client_dir),
            'steamdeps.txt',
            os.path.join(tmpdir, 'bootstrap', ''),
            executable=False,
        )
        self.install_search(
            (self.client_overlay, client_dir),
            'ubuntu12_32/steam',
            os.path.join(tmpdir, 'bootstrap', 'ubuntu12_32', ''),
            executable=True,
        )
        self.install_search(
            (self.client_overlay, client_dir),
            'ubuntu12_32/crashhandler.so',
            os.path.join(tmpdir, 'bootstrap', 'ubuntu12_32', ''),
            executable=True,
        )

        for font in (
            'GoNotoKurrent-Bold.ttf',
            'GoNotoKurrent-Regular.ttf',
        ):
            self.install_search(
                (self.client_overlay, client_dir),
                'clientui/fonts/' + font,
                os.path.join(tmpdir, 'bootstrap', 'clientui', 'fonts', ''),
            )

        runtimedir = os.path.join(client_dir, 'ubuntu12_32')
        bootstrap = os.path.join(tmpdir, 'bootstrap')
        bootstrap_runtime_dir = os.path.join(bootstrap, 'ubuntu12_32')

        with tarfile.open(
            os.path.join(runtimedir, 'steam-runtime.tar.xz'), mode='r|xz',
        ) as tar_reader:
            for info in tar_reader:
                bits = info.name.split('/')

                if bits[0] != 'steam-runtime':
                    raise ValueError(f'{info.name} is not in steam-runtime/')

                if '..' in bits:
                    raise ValueError(f'{info.name} has path traversal')

                if self.want_file_from_runtime(bits[1:]):
                    tar_reader.extract(info, path=bootstrap_runtime_dir)

        with tarfile.open(
            os.path.join(
                self.destination,
                'bootstraplinux_ubuntu12_32.tar.xz',
            ),
            mode='w|xz',
            format=tarfile.GNU_FORMAT,
        ) as tar_writer:
            members = []

            for dir_path, dirs, files in os.walk(
                bootstrap,
                topdown=True,
                followlinks=False,
            ):
                rel_dir_path = os.path.relpath(dir_path, bootstrap)

                for member in dirs + files:
                    if rel_dir_path == '.':
                        members.append(member)
                    else:
                        members.append(os.path.join(rel_dir_path, member))

            for member in sorted(members):
                tar_writer.add(
                    os.path.join(bootstrap, member),
                    arcname=member,
                    recursive=False,
                    filter=self._normalize_tar_entry,
                )

    def run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            if self.client_dir is not None:
                client_dir = self.client_dir
            else:
                client_dir = os.path.join(tmpdir, 'client')
                self.download_client(tmpdir)
                self.ensure_scout_tarball(tmpdir)

            self.build_bootstrap(client_dir, tmpdir)

        info = {
            'client_version': self.client_version,
            'runtime_version': self.resolved_runtime,
        }
        with open(
            os.path.join(self.destination, 'client-versions.json'),
            'w'
        ) as writer:
            json.dump(info, writer, indent=2)
            writer.write('\n')


def main() -> None:
    logging.basicConfig()
    logging.getLogger().setLevel(logging.DEBUG)

    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '--beta-universe', default=False,
        help='Build a steambeta package',
    )
    parser.add_argument(
        '--client-dir', default=None,
        help=(
            'Use a pre-downloaded Steam Client instead of downloading it '
            'from the Steam CDN'
        ),
    )
    parser.add_argument(
        '--client-uri', default='https://steamcdn-a.akamaihd.net/client',
        help='Base URI for Steam client files',
    )
    parser.add_argument(
        '--client-manifest', default='steam_client_ubuntu12',
        help='Client manifest VDF file relative to CLIENT_URI',
    )
    parser.add_argument(
        '--client-overlay', default='',
        help='Directory with files to override in bootstrap tarball',
    )
    parser.add_argument(
        '--client-tarball-uri', default=None,
        help=(
            'Download Steam Client files from this URI instead of from '
            'the Steam client CDN'
        ),
    )
    parser.add_argument(
        '--credential-env', dest='credential_envs', action='append',
        default=[], metavar='HOSTNAME=VARIABLE',
        help=(
            'Evaluate environment variable VARIABLE to get the '
            'username:password to use for https://HOSTNAME, '
            'for example `export SERVER_CREDS=gfreeman:n1h1l4nth` '
            'and use --credential-env=server.example.com=SERVER_CREDS'
        ),
    )
    parser.add_argument(
        '--runtime-snapshots-uri',
        default=(
            'https://repo.steampowered.com/steamrt1/images'
        ),
        help=(
            'Download Steam Runtime tarball from a subdirectory of this'
        ),
    )
    parser.add_argument(
        '--runtime-version', default='',
        help='Replace Steam Runtime tarball (if any) with this version',
    )

    parser.add_argument(
        '--destination',
        default=os.path.dirname(os.path.dirname(__file__)),
        help='Directory containing Steam launcher deb packaging',
    )

    try:
        args = parser.parse_args()
        Main(**vars(args)).run()
    except InvocationError as e:
        parser.error(str(e))


if __name__ == '__main__':
    main()
