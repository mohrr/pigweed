#!/usr/bin/env python
# Copyright 2020 The Pigweed Authors
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations under
# the License.
"""Watch files for changes and rebuild.

pw watch runs Ninja in a build directory when source files change. It works with
any Ninja project (GN or CMake).

Usage examples:

  # Find a build directory and build the default target
  pw watch

  # Find a build directory and build the stm32f429i target
  pw watch python.lint stm32f429i

  # Build pw_run_tests.modules in the out/cmake directory
  pw watch -C out/cmake pw_run_tests.modules

  # Build the default target in out/ and pw_apps in out/cmake
  pw watch -C out -C out/cmake pw_apps

  # Find a directory and build python.tests, and build pw_apps in out/cmake
  pw watch python.tests -C out/cmake pw_apps
"""

import argparse
from dataclasses import dataclass
import errno
from itertools import zip_longest
import logging
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import threading
from threading import Thread
from typing import (
    Iterable,
    List,
    NamedTuple,
    NoReturn,
    Optional,
    Sequence,
    Tuple,
)

try:
    import httpwatcher  # type: ignore[import]
except ImportError:
    httpwatcher = None

from watchdog.events import FileSystemEventHandler  # type: ignore[import]
from watchdog.observers import Observer  # type: ignore[import]

from prompt_toolkit import prompt
from prompt_toolkit.formatted_text.base import OneStyleAndTextTuple
from prompt_toolkit.formatted_text import StyleAndTextTuples

import pw_cli.branding
import pw_cli.color
import pw_cli.env
import pw_cli.log
import pw_cli.plugins
import pw_console.python_logging

from pw_watch.watch_app import WatchApp
from pw_watch.debounce import DebouncedFunction, Debouncer

_COLOR = pw_cli.color.colors()
_LOG = logging.getLogger('pw_watch')
_NINJA_LOG = logging.getLogger('pw_watch_ninja_output')
_ERRNO_INOTIFY_LIMIT_REACHED = 28

# Suppress events under 'fsevents', generated by watchdog on every file
# event on MacOS.
# TODO(b/182281481): Fix file ignoring, rather than just suppressing logs
_FSEVENTS_LOG = logging.getLogger('fsevents')
_FSEVENTS_LOG.setLevel(logging.WARNING)

_PASS_MESSAGE = """
  ██████╗  █████╗ ███████╗███████╗██╗
  ██╔══██╗██╔══██╗██╔════╝██╔════╝██║
  ██████╔╝███████║███████╗███████╗██║
  ██╔═══╝ ██╔══██║╚════██║╚════██║╚═╝
  ██║     ██║  ██║███████║███████║██╗
  ╚═╝     ╚═╝  ╚═╝╚══════╝╚══════╝╚═╝
"""

# Pick a visually-distinct font from "PASS" to ensure that readers can't
# possibly mistake the difference between the two states.
_FAIL_MESSAGE = """
   ▄██████▒░▄▄▄       ██▓  ░██▓
  ▓█▓     ░▒████▄    ▓██▒  ░▓██▒
  ▒████▒   ░▒█▀  ▀█▄  ▒██▒ ▒██░
  ░▓█▒    ░░██▄▄▄▄██ ░██░  ▒██░
  ░▒█░      ▓█   ▓██▒░██░░ ████████▒
   ▒█░      ▒▒   ▓▒█░░▓  ░  ▒░▓  ░
   ░▒        ▒   ▒▒ ░ ▒ ░░  ░ ▒  ░
   ░ ░       ░   ▒    ▒ ░   ░ ░
                 ░  ░ ░       ░  ░
"""

_FULLSCREEN_STATUS_COLUMN_WIDTH = 10


# TODO(keir): Figure out a better strategy for exiting. The problem with the
# watcher is that doing a "clean exit" is slow. However, by directly exiting,
# we remove the possibility of the wrapper script doing anything on exit.
def _die(*args) -> NoReturn:
    _LOG.critical(*args)
    sys.exit(1)


class WatchCharset(NamedTuple):
    slug_ok: str
    slug_fail: str


_ASCII_CHARSET = WatchCharset(_COLOR.green('OK  '), _COLOR.red('FAIL'))
_EMOJI_CHARSET = WatchCharset('✔️ ', '💥')


@dataclass(frozen=True)
class BuildCommand:
    build_dir: Path
    targets: Tuple[str, ...] = ()

    def args(self) -> Tuple[str, ...]:
        return (str(self.build_dir), *self.targets)

    def __str__(self) -> str:
        return ' '.join(shlex.quote(arg) for arg in self.args())


def git_ignored(file: Path) -> bool:
    """Returns true if this file is in a Git repo and ignored by that repo.

    Returns true for ignored files that were manually added to a repo.
    """
    file = file.resolve()
    directory = file.parent

    # Run the Git command from file's parent so that the correct repo is used.
    while True:
        try:
            returncode = subprocess.run(
                ['git', 'check-ignore', '--quiet', '--no-index', file],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=directory).returncode
            return returncode in (0, 128)
        except FileNotFoundError:
            # If the directory no longer exists, try parent directories until
            # an existing directory is found or all directories have been
            # checked. This approach makes it possible to check if a deleted
            # path is ignored in the repo it was originally created in.
            if directory == directory.parent:
                return False

            directory = directory.parent


class PigweedBuildWatcher(FileSystemEventHandler, DebouncedFunction):
    """Process filesystem events and launch builds if necessary."""
    # pylint: disable=too-many-instance-attributes
    NINJA_BUILD_STEP = re.compile(
        r'^\[(?P<step>[0-9]+)/(?P<total_steps>[0-9]+)\] (?P<action>.*)$')

    def __init__(
        self,
        build_commands: Sequence[BuildCommand],
        patterns: Sequence[str] = (),
        ignore_patterns: Sequence[str] = (),
        charset: WatchCharset = _ASCII_CHARSET,
        restart: bool = True,
        jobs: Optional[int] = None,
        fullscreen: bool = False,
        banners: bool = True,
        keep_going: bool = False,
    ):
        super().__init__()

        self.banners = banners
        self.status_message: Optional[OneStyleAndTextTuple] = None
        self.result_message: Optional[StyleAndTextTuples] = None
        self.current_stdout = ''
        self.current_build_step = ''
        self.current_build_percent = 0.0
        self.current_build_errors = 0
        self.patterns = patterns
        self.ignore_patterns = ignore_patterns
        self.build_commands = build_commands
        self.charset: WatchCharset = charset

        self.restart_on_changes = restart
        self.fullscreen_enabled = fullscreen
        self.watch_app: Optional[WatchApp] = None

        # Initialize self._current_build to an empty subprocess.
        self._current_build = subprocess.Popen('',
                                               shell=True,
                                               errors='replace')

        self._extra_ninja_args = [] if jobs is None else [f'-j{jobs}']
        if keep_going:
            self._extra_ninja_args.extend(['-k', '0'])

        self.debouncer = Debouncer(self)

        # Track state of a build. These need to be members instead of locals
        # due to the split between dispatch(), run(), and on_complete().
        self.matching_path: Optional[Path] = None
        self.builds_succeeded: List[bool] = []

        if not self.fullscreen_enabled:
            self.wait_for_keypress_thread = threading.Thread(
                None, self._wait_for_enter)
            self.wait_for_keypress_thread.start()

    def rebuild(self):
        """Rebuild command triggered from watch app."""
        self._current_build.terminate()
        self._current_build.wait()
        self.debouncer.press('Manual build requested')

    def _wait_for_enter(self) -> NoReturn:
        try:
            while True:
                _ = prompt('')
                self.rebuild()
        # Ctrl-C on Unix generates KeyboardInterrupt
        # Ctrl-Z on Windows generates EOFError
        except (KeyboardInterrupt, EOFError):
            # Force stop any running ninja builds.
            if self._current_build:
                self._current_build.terminate()
            _exit_due_to_interrupt()

    def _path_matches(self, path: Path) -> bool:
        """Returns true if path matches according to the watcher patterns"""
        return (not any(path.match(x) for x in self.ignore_patterns)
                and any(path.match(x) for x in self.patterns))

    def dispatch(self, event) -> None:
        # There isn't any point in triggering builds on new directory creation.
        # It's the creation or modification of files that indicate something
        # meaningful enough changed for a build.
        if event.is_directory:
            return

        # Collect paths of interest from the event.
        paths: List[str] = []
        if hasattr(event, 'dest_path'):
            paths.append(os.fsdecode(event.dest_path))
        if event.src_path:
            paths.append(os.fsdecode(event.src_path))
        for raw_path in paths:
            _LOG.debug('File event: %s', raw_path)

        # Check whether Git cares about any of these paths.
        for path in (Path(p).resolve() for p in paths):
            if not git_ignored(path) and self._path_matches(path):
                self._handle_matched_event(path)
                return

    def _handle_matched_event(self, matching_path: Path) -> None:
        if self.matching_path is None:
            self.matching_path = matching_path

        log_message = f'File change detected: {os.path.relpath(matching_path)}'
        if self.restart_on_changes:
            if self.fullscreen_enabled and self.watch_app:
                self.watch_app.rebuild_on_filechange()
            self.debouncer.press(f'{log_message} Triggering build...')
        else:
            _LOG.info('%s ; not rebuilding', log_message)

    def _clear_screen(self) -> None:
        if not self.fullscreen_enabled:
            print('\033c', end='')  # TODO(pwbug/38): Not Windows compatible.
            sys.stdout.flush()

    # Implementation of DebouncedFunction.run()
    #
    # Note: This will run on the timer thread created by the Debouncer, rather
    # than on the main thread that's watching file events. This enables the
    # watcher to continue receiving file change events during a build.
    def run(self) -> None:
        """Run all the builds in serial and capture pass/fail for each."""

        # Clear the screen and show a banner indicating the build is starting.
        self._clear_screen()

        if self.fullscreen_enabled:
            self.create_result_message()
            _LOG.info(
                _COLOR.green(
                    'Watching for changes. Ctrl-d to exit; enter to rebuild'))
        else:
            for line in pw_cli.branding.banner().splitlines():
                _LOG.info(line)
            _LOG.info(
                _COLOR.green(
                    '  Watching for changes. Ctrl-C to exit; enter to rebuild')
            )
        _LOG.info('')
        _LOG.info('Change detected: %s', self.matching_path)

        self._clear_screen()

        self.builds_succeeded = []
        num_builds = len(self.build_commands)
        _LOG.info('Starting build with %d directories', num_builds)

        env = os.environ.copy()
        # Force colors in Pigweed subcommands run through the watcher.
        env['PW_USE_COLOR'] = '1'
        # Force Ninja to output ANSI colors
        env['CLICOLOR_FORCE'] = '1'

        for i, cmd in enumerate(self.build_commands, 1):
            index = f'[{i}/{num_builds}]'
            self.builds_succeeded.append(self._run_build(index, cmd, env))
            if self.builds_succeeded[-1]:
                level = logging.INFO
                tag = '(OK)'
            else:
                level = logging.ERROR
                tag = '(FAIL)'

            _LOG.log(level, '%s Finished build: %s %s', index, cmd, tag)
            self.create_result_message()

    def create_result_message(self):
        if not self.fullscreen_enabled:
            return

        self.result_message = []
        first_building_target_found = False
        for (succeeded, command) in zip_longest(self.builds_succeeded,
                                                self.build_commands):
            if succeeded:
                self.result_message.append(
                    ('class:theme-fg-green',
                     'OK'.rjust(_FULLSCREEN_STATUS_COLUMN_WIDTH)))
            elif succeeded is None and not first_building_target_found:
                first_building_target_found = True
                self.result_message.append(
                    ('class:theme-fg-yellow',
                     'Building'.rjust(_FULLSCREEN_STATUS_COLUMN_WIDTH)))
            elif first_building_target_found:
                self.result_message.append(
                    ('', ''.rjust(_FULLSCREEN_STATUS_COLUMN_WIDTH)))
            else:
                self.result_message.append(
                    ('class:theme-fg-red',
                     'Failed'.rjust(_FULLSCREEN_STATUS_COLUMN_WIDTH)))
            self.result_message.append(('', f'  {command}\n'))

    def _run_build(self, index: str, cmd: BuildCommand, env: dict) -> bool:
        # Make sure there is a build.ninja file for Ninja to use.
        build_ninja = cmd.build_dir / 'build.ninja'
        if not build_ninja.exists():
            # If this is a CMake directory, prompt the user to re-run CMake.
            if cmd.build_dir.joinpath('CMakeCache.txt').exists():
                _LOG.error('%s %s does not exist; re-run CMake to generate it',
                           index, build_ninja)
                return False

            if not cmd.build_dir.joinpath('args.gn').exists():
                _LOG.error(
                    '%s %s does not exist; run GN or CMake in %s to generate '
                    'it', index, build_ninja, cmd.build_dir)
                return False

            _LOG.warning('%s %s does not exist; running gn gen %s', index,
                         build_ninja, cmd.build_dir)
            if not self._execute_command(['gn', 'gen', cmd.build_dir], env):
                return False

        command = ['ninja', *self._extra_ninja_args, '-C', *cmd.args()]
        _LOG.info('%s Starting build: %s', index,
                  ' '.join(shlex.quote(arg) for arg in command))

        return self._execute_command(command, env)

    def _execute_command(self, command: list, env: dict) -> bool:
        """Runs a command with a blank before/after for visual separation."""
        self.current_build_errors = 0
        self.status_message = (
            'class:theme-fg-yellow',
            'Building'.rjust(_FULLSCREEN_STATUS_COLUMN_WIDTH))
        if self.fullscreen_enabled:
            return self._execute_command_watch_app(command, env)
        print()
        self._current_build = subprocess.Popen(command,
                                               env=env,
                                               errors='replace')
        returncode = self._current_build.wait()
        print()
        return returncode == 0

    def _execute_command_watch_app(self, command: list, env: dict) -> bool:
        """Runs a command with and outputs the logs."""
        if not self.watch_app:
            return False
        self.current_stdout = ''
        returncode = None
        with subprocess.Popen(command,
                              env=env,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT,
                              errors='replace') as proc:
            self._current_build = proc

            # Empty line at the start.
            _NINJA_LOG.info('')
            while returncode is None:
                if not proc.stdout:
                    continue

                output = proc.stdout.readline()
                self.current_stdout += output

                line_match_result = self.NINJA_BUILD_STEP.match(output)
                if line_match_result:
                    matches = line_match_result.groupdict()
                    self.current_build_step = line_match_result.group(0)
                    self.current_build_percent = float(
                        int(matches.get('step', 0)) /
                        int(matches.get('total_steps', 1)))

                elif output.startswith(WatchApp.NINJA_FAILURE_TEXT):
                    _NINJA_LOG.critical(output.strip())
                    self.current_build_errors += 1

                else:
                    # Mypy output mixes character encoding in its colored output
                    # due to it's use of the curses module retrieving the 'sgr0'
                    # (or exit_attribute_mode) capability from the host
                    # machine's terminfo database.
                    #
                    # This can result in this sequence ending up in STDOUT as
                    # b'\x1b(B\x1b[m'. (B tells terminals to interpret text as
                    # USASCII encoding but will appear in prompt_toolkit as a B
                    # character.
                    #
                    # The following replace calls will strip out those
                    # instances.
                    _NINJA_LOG.info(
                        output.replace('\x1b(B\x1b[m',
                                       '').replace('\x1b[1m', '').strip())
                self.watch_app.redraw_ui()

                returncode = proc.poll()
            # Empty line at the end.
            _NINJA_LOG.info('')

        return returncode == 0

    # Implementation of DebouncedFunction.cancel()
    def cancel(self) -> bool:
        if self.restart_on_changes:
            self._current_build.terminate()
            self._current_build.wait()
            return True

        return False

    # Implementation of DebouncedFunction.run()
    def on_complete(self, cancelled: bool = False) -> None:
        # First, use the standard logging facilities to report build status.
        if cancelled:
            self.status_message = (
                '', 'Cancelled'.rjust(_FULLSCREEN_STATUS_COLUMN_WIDTH))
            _LOG.error('Finished; build was interrupted')
        elif all(self.builds_succeeded):
            self.status_message = (
                'class:theme-fg-green',
                'Succeeded'.rjust(_FULLSCREEN_STATUS_COLUMN_WIDTH))
            _LOG.info('Finished; all successful')
        else:
            self.status_message = (
                'class:theme-fg-red',
                'Failed'.rjust(_FULLSCREEN_STATUS_COLUMN_WIDTH))
            _LOG.info('Finished; some builds failed')

        # Show individual build results for fullscreen app
        if self.fullscreen_enabled:
            self.create_result_message()
        # For non-fullscreen pw watch
        else:
            # Show a more distinct colored banner.
            if not cancelled:
                # Write out build summary table so you can tell which builds
                # passed and which builds failed.
                _LOG.info('')
                _LOG.info(' .------------------------------------')
                _LOG.info(' |')
                for (succeeded, cmd) in zip(self.builds_succeeded,
                                            self.build_commands):
                    slug = (self.charset.slug_ok
                            if succeeded else self.charset.slug_fail)
                    _LOG.info(' |   %s  %s', slug, cmd)
                _LOG.info(' |')
                _LOG.info(" '------------------------------------")
            else:
                # Build was interrupted.
                _LOG.info('')
                _LOG.info(' .------------------------------------')
                _LOG.info(' |')
                _LOG.info(' |  %s- interrupted', self.charset.slug_fail)
                _LOG.info(' |')
                _LOG.info(" '------------------------------------")

            # Show a large color banner for the overall result.
            if self.banners:
                if all(self.builds_succeeded) and not cancelled:
                    for line in _PASS_MESSAGE.splitlines():
                        _LOG.info(_COLOR.green(line))
                else:
                    for line in _FAIL_MESSAGE.splitlines():
                        _LOG.info(_COLOR.red(line))

        if self.watch_app:
            self.watch_app.redraw_ui()
        self.matching_path = None

    # Implementation of DebouncedFunction.on_keyboard_interrupt()
    def on_keyboard_interrupt(self) -> NoReturn:
        _exit_due_to_interrupt()


_WATCH_PATTERN_DELIMITER = ','
_WATCH_PATTERNS = (
    # keep-sorted: start ignore-case ignore-prefix=','*.
    '*.bloaty',
    '*.c',
    '*.cc',
    '*.cfg',
    '*.cmake',
    'CMakeLists.txt',
    '*.cpp',
    '*.css',
    '*.dts',
    '*.dtsi',
    '*.gn',
    '*.gni',
    '*.go',
    '*.h',
    '*.hpp',
    '*.ld',
    '*.md',
    '*.options',
    '*.proto',
    '*.py',
    '*.rs',
    '*.rst',
    '*.S',
    '*.s',
    '*.toml',
    # keep-sorted: end
)


def add_parser_arguments(parser: argparse.ArgumentParser) -> None:
    """Sets up an argument parser for pw watch."""
    parser.add_argument('--patterns',
                        help=(_WATCH_PATTERN_DELIMITER +
                              '-delimited list of globs to '
                              'watch to trigger recompile'),
                        default=_WATCH_PATTERN_DELIMITER.join(_WATCH_PATTERNS))
    parser.add_argument('--ignore_patterns',
                        dest='ignore_patterns_string',
                        help=(_WATCH_PATTERN_DELIMITER +
                              '-delimited list of globs to '
                              'ignore events from'))

    parser.add_argument('--exclude_list',
                        nargs='+',
                        type=Path,
                        help='directories to ignore during pw watch',
                        default=[])
    parser.add_argument('--no-restart',
                        dest='restart',
                        action='store_false',
                        help='do not restart ongoing builds if files change')
    parser.add_argument('-k',
                        '--keep-going',
                        action='store_true',
                        help=('Keep building past the first failure. This is '
                              'equivalent to passing "-k 0" to ninja.'))
    parser.add_argument(
        'default_build_targets',
        nargs='*',
        metavar='target',
        default=[],
        help=('Automatically locate a build directory and build these '
              'targets. For example, `host docs` searches for a Ninja '
              'build directory at out/ and builds the `host` and `docs` '
              'targets. To specify one or more directories, ust the '
              '-C / --build_directory option.'))
    parser.add_argument(
        '-C',
        '--build_directory',
        dest='build_directories',
        nargs='+',
        action='append',
        default=[],
        metavar=('directory', 'target'),
        help=('Specify a build directory and optionally targets to '
              'build. `pw watch -C out tgt` is equivalent to `ninja '
              '-C out tgt`'))
    parser.add_argument(
        '--serve-docs',
        dest='serve_docs',
        action='store_true',
        default=False,
        help=('Start a webserver for docs on localhost. The port for this '
              'webserver can be set with the --serve-docs-port option. '
              'Defaults to http://127.0.0.1:8000. This option requires '
              'the httpwatcher package to be installed.'))
    parser.add_argument(
        '--serve-docs-port',
        dest='serve_docs_port',
        type=int,
        default=8000,
        help='Set the port for the docs webserver. Default to 8000.')
    parser.add_argument(
        '--serve-docs-path',
        dest='serve_docs_path',
        type=Path,
        default="docs/gen/docs",
        help=('Set the path for the docs to serve. Default to docs/gen/docs'
              ' in the build directory.'))
    parser.add_argument(
        '-j',
        '--jobs',
        type=int,
        help="Number of cores to use; defaults to Ninja's default")
    parser.add_argument('-f',
                        '--fullscreen',
                        action='store_true',
                        default=False,
                        help='Use a fullscreen interface.')
    parser.add_argument('--debug-logging',
                        action='store_true',
                        help='Enable debug logging.')
    parser.add_argument('--no-banners',
                        dest='banners',
                        action='store_false',
                        help='Hide pass/fail banners.')


def _exit(code: int) -> NoReturn:
    # Note: The "proper" way to exit is via observer.stop(), then
    # running a join. However it's slower, so just exit immediately.
    #
    # Additionally, since there are several threads in the watcher, the usual
    # sys.exit approach doesn't work. Instead, run the low level exit which
    # kills all threads.
    os._exit(code)  # pylint: disable=protected-access


def _exit_due_to_interrupt() -> NoReturn:
    # To keep the log lines aligned with each other in the presence of
    # a '^C' from the keyboard interrupt, add a newline before the log.
    print('')
    _LOG.info('Got Ctrl-C; exiting...')
    _exit(0)


def _exit_due_to_inotify_watch_limit():
    # Show information and suggested commands in OSError: inotify limit reached.
    _LOG.error('Inotify watch limit reached: run this in your terminal if '
               'you are in Linux to temporarily increase inotify limit.  \n')
    _LOG.info(
        _COLOR.green('        sudo sysctl fs.inotify.max_user_watches='
                     '$NEW_LIMIT$\n'))
    _LOG.info('  Change $NEW_LIMIT$ with an integer number, '
              'e.g., 20000 should be enough.')
    _exit(0)


def _exit_due_to_inotify_instance_limit():
    # Show information and suggested commands in OSError: inotify limit reached.
    _LOG.error('Inotify instance limit reached: run this in your terminal if '
               'you are in Linux to temporarily increase inotify limit.  \n')
    _LOG.info(
        _COLOR.green('        sudo sysctl fs.inotify.max_user_instances='
                     '$NEW_LIMIT$\n'))
    _LOG.info('  Change $NEW_LIMIT$ with an integer number, '
              'e.g., 20000 should be enough.')
    _exit(0)


def _exit_due_to_pigweed_not_installed():
    # Show information and suggested commands when pigweed environment variable
    # not found.
    _LOG.error('Environment variable $PW_ROOT not defined or is defined '
               'outside the current directory.')
    _LOG.error('Did you forget to activate the Pigweed environment? '
               'Try source ./activate.sh')
    _LOG.error('Did you forget to install the Pigweed environment? '
               'Try source ./bootstrap.sh')
    _exit(1)


# Go over each directory inside of the current directory.
# If it is not on the path of elements in directories_to_exclude, add
# (directory, True) to subdirectories_to_watch and later recursively call
# Observer() on them.
# Otherwise add (directory, False) to subdirectories_to_watch and later call
# Observer() with recursion=False.
def minimal_watch_directories(to_watch: Path, to_exclude: Iterable[Path]):
    """Determine which subdirectory to watch recursively"""
    try:
        to_watch = Path(to_watch)
    except TypeError:
        assert False, "Please watch one directory at a time."

    # Reformat to_exclude.
    directories_to_exclude: List[Path] = [
        to_watch.joinpath(directory_to_exclude)
        for directory_to_exclude in to_exclude
        if to_watch.joinpath(directory_to_exclude).is_dir()
    ]

    # Split the relative path of directories_to_exclude (compared to to_watch),
    # and generate all parent paths needed to be watched without recursion.
    exclude_dir_parents = {to_watch}
    for directory_to_exclude in directories_to_exclude:
        parts = list(
            Path(directory_to_exclude).relative_to(to_watch).parts)[:-1]
        dir_tmp = to_watch
        for part in parts:
            dir_tmp = Path(dir_tmp, part)
            exclude_dir_parents.add(dir_tmp)

    # Go over all layers of directory. Append those that are the parents of
    # directories_to_exclude to the list with recursion==False, and others
    # with recursion==True.
    for directory in exclude_dir_parents:
        dir_path = Path(directory)
        yield dir_path, False
        for item in Path(directory).iterdir():
            if (item.is_dir() and item not in exclude_dir_parents
                    and item not in directories_to_exclude):
                yield item, True


def get_common_excludes() -> List[Path]:
    """Find commonly excluded directories, and return them as a [Path]"""
    exclude_list: List[Path] = []

    typical_ignored_directories: List[str] = [
        '.environment',  # Legacy bootstrap-created CIPD and Python venv.
        '.presubmit',  # Presubmit-created CIPD and Python venv.
        '.git',  # Pigweed's git repo.
        '.mypy_cache',  # Python static analyzer.
        '.cargo',  # Rust package manager.
        'environment',  # Bootstrap-created CIPD and Python venv.
        'out',  # Typical build directory.
    ]

    # Preset exclude list for Pigweed's upstream directories.
    pw_root_dir = Path(os.environ['PW_ROOT'])
    exclude_list.extend(pw_root_dir / ignored_directory
                        for ignored_directory in typical_ignored_directories)

    # Preset exclude for common downstream project structures.
    #
    # If watch is invoked outside of the Pigweed root, exclude common
    # directories.
    pw_project_root_dir = Path(os.environ['PW_PROJECT_ROOT'])
    if pw_project_root_dir != pw_root_dir:
        exclude_list.extend(
            pw_project_root_dir / ignored_directory
            for ignored_directory in typical_ignored_directories)

    # Check for and warn about legacy directories.
    legacy_directories = [
        '.cipd',  # Legacy CIPD location.
        '.python3-venv',  # Legacy Python venv location.
    ]
    found_legacy = False
    for legacy_directory in legacy_directories:
        full_legacy_directory = pw_root_dir / legacy_directory
        if full_legacy_directory.is_dir():
            _LOG.warning('Legacy environment directory found: %s',
                         str(full_legacy_directory))
            exclude_list.append(full_legacy_directory)
            found_legacy = True
    if found_legacy:
        _LOG.warning('Found legacy environment directory(s); these '
                     'should be deleted')

    return exclude_list


def _serve_docs(build_dir: Path, serve_docs_port: int,
                serve_docs_path: Path) -> None:
    if httpwatcher is None:
        _LOG.warning(
            '--serve-docs was specified, but httpwatcher is not available')
        _LOG.info('Install httpwatcher to use --serve-docs')
        return

    def httpwatcher_thread():
        # Disable logs from httpwatcher and deps
        logging.getLogger('httpwatcher').setLevel(logging.CRITICAL)
        logging.getLogger('tornado').setLevel(logging.CRITICAL)

        docs_path = build_dir.joinpath(serve_docs_path.joinpath('html'))
        httpwatcher.watch(docs_path, host='127.0.0.1', port=serve_docs_port)

    # Spin up an httpwatcher in a new thread since it blocks
    threading.Thread(None, httpwatcher_thread, 'httpwatcher').start()


def watch_setup(
    default_build_targets: List[str],
    build_directories: List[str],
    patterns: str,
    ignore_patterns_string: str,
    exclude_list: List[Path],
    restart: bool,
    jobs: Optional[int],
    serve_docs: bool,
    serve_docs_port: int,
    serve_docs_path: Path,
    fullscreen: bool,
    banners: bool,
    keep_going: bool,
    debug_logging: bool,  # pylint: disable=unused-argument
    # pylint: disable=too-many-arguments
) -> Tuple[str, PigweedBuildWatcher, List[Path]]:
    """Watches files and runs Ninja commands when they change."""
    _LOG.info('Starting Pigweed build watcher')

    # Get pigweed directory information from environment variable PW_ROOT.
    if os.environ['PW_ROOT'] is None:
        _exit_due_to_pigweed_not_installed()
    pw_root = Path(os.environ['PW_ROOT']).resolve()
    if Path.cwd().resolve() not in [pw_root, *pw_root.parents]:
        _exit_due_to_pigweed_not_installed()

    # Preset exclude list for pigweed directory.
    exclude_list += get_common_excludes()
    # Add build directories to the exclude list.
    exclude_list.extend(
        Path(build_dir[0]).resolve() for build_dir in build_directories)

    build_commands = [
        BuildCommand(Path(build_dir[0]), tuple(build_dir[1:]))
        for build_dir in build_directories
    ]

    # If no build directory was specified, check for out/build.ninja.
    if default_build_targets or not build_directories:
        # Make sure we found something; if not, bail.
        if not Path('out').exists():
            _die("No build dirs found. Did you forget to run 'gn gen out'?")

        build_commands.append(
            BuildCommand(Path('out'), tuple(default_build_targets)))

    # Verify that the build output directories exist.
    for i, build_target in enumerate(build_commands, 1):
        if not build_target.build_dir.is_dir():
            _die("Build directory doesn't exist: %s", build_target)
        else:
            _LOG.info('Will build [%d/%d]: %s', i, len(build_commands),
                      build_target)

    _LOG.debug('Patterns: %s', patterns)

    if serve_docs:
        _serve_docs(build_commands[0].build_dir, serve_docs_port,
                    serve_docs_path)

    # Try to make a short display path for the watched directory that has
    # "$HOME" instead of the full home directory. This is nice for users
    # who have deeply nested home directories.
    path_to_log = str(Path().resolve()).replace(str(Path.home()), '$HOME')

    # Ignore the user-specified patterns.
    ignore_patterns = (ignore_patterns_string.split(_WATCH_PATTERN_DELIMITER)
                       if ignore_patterns_string else [])

    env = pw_cli.env.pigweed_environment()
    if env.PW_EMOJI:
        charset = _EMOJI_CHARSET
    else:
        charset = _ASCII_CHARSET

    event_handler = PigweedBuildWatcher(
        build_commands=build_commands,
        patterns=patterns.split(_WATCH_PATTERN_DELIMITER),
        ignore_patterns=ignore_patterns,
        charset=charset,
        restart=restart,
        jobs=jobs,
        fullscreen=fullscreen,
        banners=banners,
        keep_going=keep_going,
    )
    return path_to_log, event_handler, exclude_list


def watch(path_to_log: Path, event_handler: PigweedBuildWatcher,
          exclude_list: List[Path]):
    """Watches files and runs Ninja commands when they change."""
    try:
        # It can take awhile to configure the filesystem watcher, so have the
        # message reflect that with the "...". Run inside the try: to
        # gracefully handle the user Ctrl-C'ing out during startup.

        _LOG.info('Attaching filesystem watcher to %s/...', path_to_log)

        # Observe changes for all files in the root directory. Whether the
        # directory should be observed recursively or not is determined by the
        # second element in subdirectories_to_watch.
        observers = []
        for path, rec in minimal_watch_directories(Path.cwd(), exclude_list):
            observer = Observer()
            observer.schedule(
                event_handler,
                str(path),
                recursive=rec,
            )
            observer.start()
            observers.append(observer)

        event_handler.debouncer.press('Triggering initial build...')
        for observer in observers:
            while observer.is_alive():
                observer.join(1)

    # Ctrl-C on Unix generates KeyboardInterrupt
    # Ctrl-Z on Windows generates EOFError
    except (KeyboardInterrupt, EOFError):
        _exit_due_to_interrupt()
    except OSError as err:
        if err.args[0] == _ERRNO_INOTIFY_LIMIT_REACHED:
            _exit_due_to_inotify_watch_limit()
        if err.errno == errno.EMFILE:
            _exit_due_to_inotify_instance_limit()
        raise err

    _LOG.critical('Should never get here')
    observer.join()


def main() -> None:
    """Watch files for changes and rebuild."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    add_parser_arguments(parser)
    args = parser.parse_args()

    path_to_log, event_handler, exclude_list = watch_setup(**vars(args))

    if args.fullscreen:
        watch_logfile = (pw_console.python_logging.create_temp_log_file(
            prefix=__package__))
        pw_cli.log.install(
            level=logging.DEBUG,
            use_color=True,
            hide_timestamp=False,
            log_file=watch_logfile,
        )
        pw_console.python_logging.setup_python_logging(
            last_resort_filename=watch_logfile)

        watch_thread = Thread(target=watch,
                              args=(path_to_log, event_handler, exclude_list),
                              daemon=True)
        watch_thread.start()
        watch_app = WatchApp(event_handler=event_handler,
                             debug_logging=args.debug_logging,
                             log_file_name=watch_logfile)

        event_handler.watch_app = watch_app
        watch_app.run()
    else:
        pw_cli.log.install(
            level=logging.DEBUG if args.debug_logging else logging.INFO,
            use_color=True,
            hide_timestamp=False,
        )
        watch(Path(path_to_log), event_handler, exclude_list)


if __name__ == '__main__':
    main()
