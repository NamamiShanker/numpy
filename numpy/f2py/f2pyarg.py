#!/usr/bin/env python3

"""
argparse+logging front-end to f2py

The concept is based around the idea that F2PY is overloaded in terms of
functionality:

1. Generating `.pyf` signature files
2. Creating the wrapper `.c` files
3. Compilation helpers
  a. This essentially means `numpy.distutils` for now

The three functionalities are largely independent of each other, hence the
implementation in terms of subparsers
"""

from __future__ import annotations

import argparse
import logging
import os
import pathlib
import enum

from numpy.version import version as __version__

from .service import check_dccomp, check_npfcomp, check_dir, generate_files, segregate_files, get_f2py_modulename, wrapper_settings, compile_dist
from .utils import open_build_dir
from .auxfuncs import outmess
from .backends import backends, Backend

##################
# Temp Variables #
##################

# TODO: Kill these np.distutil specific variables
npd_link = ['atlas', 'atlas_threads', 'atlas_blas', 'atlas_blas_threads',
            'lapack_atlas', 'lapack_atlas_threads', 'atlas_3_10',
            'atlas_3_10_threads', 'atlas_3_10_blas', 'atlas_3_10_blas_threads'
            'lapack_atlas_3_10', 'lapack_atlas_3_10_threads', 'flame', 'mkl',
            'openblas', 'openblas_lapack', 'openblas_clapack', 'blis',
            'lapack_mkl', 'blas_mkl', 'accelerate', 'openblas64_',
            'openblas64__lapack', 'openblas_ilp64', 'openblas_ilp64_lapack'
            'x11', 'fft_opt', 'fftw', 'fftw2', 'fftw3', 'dfftw', 'sfftw',
            'fftw_threads', 'dfftw_threads', 'sfftw_threads', 'djbfft', 'blas',
            'lapack', 'lapack_src', 'blas_src', 'numpy', 'f2py', 'Numeric',
            'numeric', 'numarray', 'numerix', 'lapack_opt', 'lapack_ilp64_opt',
            'lapack_ilp64_plain_opt', 'lapack64__opt', 'blas_opt',
            'blas_ilp64_opt', 'blas_ilp64_plain_opt', 'blas64__opt',
            'boost_python', 'agg2', 'wx', 'gdk_pixbuf_xlib_2',
            'gdk-pixbuf-xlib-2.0', 'gdk_pixbuf_2', 'gdk-pixbuf-2.0', 'gdk',
            'gdk_2', 'gdk-2.0', 'gdk_x11_2', 'gdk-x11-2.0', 'gtkp_x11_2',
            'gtk+-x11-2.0', 'gtkp_2', 'gtk+-2.0', 'xft', 'freetype2', 'umfpack',
            'amd']

debug_api = ['capi']


# TODO: Compatibility helper, kill later
# From 3.9 onwards should be argparse.BooleanOptionalAction
class BoolAction(argparse.Action):
    """A custom action to mimic Ruby's --[no]-blah functionality in f2py

    This is meant to use ``argparse`` with a custom action to ensure backwards
    compatibility with ``f2py``. Kanged `from here`_.

    .. note::

       Like in the old ``f2py``, it is not an error to pass both variants of
       the flag, the last one will be used

    .. from here:
        https://thisdataguy.com/2017/07/03/no-options-with-argparse-and-python/
    """

    def __init__(self, option_strings, dest, nargs=None, **kwargs):
        """Initialization of the boolean flag

        Mimics the parent
        """
        super(BoolAction, self).__init__(option_strings, dest, nargs=0, **kwargs)

    def __call__(self, parser, namespace, values, option_string: str=None):
        """The logical negation action

        Essentially this returns the semantically valid operation implied by
        --no
        """
        setattr(namespace, self.dest, "no" not in option_string)


# TODO: Generalize or kill this np.distutils specific helper action class
class NPDLinkHelper(argparse.Action):
    """A custom action to work with f2py's --link-blah

    This is effectively the same as storing help_link

    """

    def __init__(self, option_strings, dest, nargs=None, **kwargs):
        """Initialization of the boolean flag

        Mimics the parent
        """
        super(NPDLinkHelper, self).__init__(option_strings, dest, nargs="*", **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        """The storage action

        Essentially, split the value on -, store in dest

        """
        items = getattr(namespace, self.dest) or []
        outvar = option_string.split("--link-")[1]
        if outvar in npd_link:
            # replicate the extend functionality
            items.append(outvar)
            setattr(namespace, self.dest, items)
        else:
            raise RuntimeError(f"{outvar} is not in {npd_link}")

class DebugLinkHelper(argparse.Action):
    """A custom action to work with f2py's --debug-blah"""

    def __call__(self, parser, namespace, values, option_string=None):
        """The storage action

        Essentially, split the value on -, store in dest

        """
        items = getattr(namespace, self.dest) or []
        outvar = option_string.split("--debug-")[1]
        if outvar in debug_api:
            items.append(outvar)
            setattr(namespace, self.dest, items)
        else:
            raise RuntimeError(f"{outvar} is not in {debug_api}")

class ProcessMacros(argparse.Action):
    """Process macros in the form of -Dmacro=value and -Dmacro"""

    def __init__(self, option_strings, dest, nargs="*", **kwargs):
        """Initialization of the boolean flag

        Mimics the parent
        """
        super(ProcessMacros, self).__init__(option_strings, dest, nargs="*", **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        """The storage action

        Essentially, split the value on -D, store in dest

        """
        items = getattr(namespace, self.dest) or []
        for value in values:
            if('=' in value):
                items.append((value.split("=")[0], value.split("=")[1]))
            else:
                items.append((value, None))
        setattr(namespace, self.dest, items)

class EnumAction(argparse.Action):
    """
    Argparse action for handling Enums
    """
    def __init__(self, **kwargs):
        # Pop off the type value
        enum_type = kwargs.pop("type", None)

        # Ensure an Enum subclass is provided
        if enum_type is None:
            raise ValueError("type must be assigned an Enum when using EnumAction")
        if not issubclass(enum_type, enum.Enum):
            raise TypeError("type must be an Enum when using EnumAction")

        # Generate choices from the Enum
        kwargs.setdefault("choices", tuple(e.value for e in enum_type))

        super(EnumAction, self).__init__(**kwargs)

        self._enum = enum_type

    def __call__(self, parser, namespace, values, option_string=None):
        # Convert value back into an Enum
        value = self._enum(values)
        setattr(namespace, self.dest, value)

class IncludePathAction(argparse.Action):
    """Custom action to extend paths when --include-paths <path1>:<path2> is called"""
    def __init__(self, option_strings, dest, nargs="?", **kwargs):
        """Initialization of the --include-paths flag

        Mimics the parent
        """
        super(IncludePathAction, self).__init__(option_strings, dest, nargs="?", **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        """Split the paths by ':' convert them to path and append them to the attribute"""
        items = getattr(namespace, self.dest) or []
        if values:
            items.extend([pathlib.Path(path) for path in values.split(os.pathsep)])
        setattr(namespace, self.dest, items)

class ParseStringFlags(argparse.Action):
    """Custom action to parse and store flags passed as string
    Ex-
    f2py --opt="-DDEBUG=1 -O" will be stored as ["-DDEBUG=1", -O]"""

    def __init__(self, option_strings, dest, nargs="1", **kwargs):
        """Initialization of the flag, mimics the parent"""
        super(ParseStringFlags, self).__init__(option_strings, dest, nargs=1, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        """The storage action, mimics the parent"""
        items = getattr(namespace, self.dest) or []
        items.extend(value.split(' ') for value in values)
        setattr(namespace, self.dest, items)

class Backends(enum.Enum):
    Meson = "meson"
    Distutils = "distutils"

##########
# Parser #
##########


parser = argparse.ArgumentParser(
    prog="f2py",
    description="""
    This program generates a Python C/API file (<modulename>module.c) that
    contains wrappers for given fortran functions so that they can be called
    from Python.

    With the -c option the corresponding extension modules are built.""",
    add_help=False,  # Since -h is taken...
    # Format to look like f2py
    formatter_class=lambda prog: argparse.RawDescriptionHelpFormatter(
        prog, max_help_position=100, width=85
    ),
    epilog=f"""
    Using the following macros may be required with non-gcc Fortran
  compilers:
    -DPREPEND_FORTRAN -DNO_APPEND_FORTRAN -DUPPERCASE_FORTRAN
    -DUNDERSCORE_G77

  When using -DF2PY_REPORT_ATEXIT, a performance report of F2PY
  interface is printed out at exit (platforms: Linux).

  When using -DF2PY_REPORT_ON_ARRAY_COPY=<int>, a message is
  sent to stderr whenever F2PY interface makes a copy of an
  array. Integer <int> sets the threshold for array sizes when
  a message should be shown.

    Version:     {__version__}
    numpy Version: {__version__}
    Requires:    Python 3.5 or higher.
    License:     NumPy license (see LICENSE.txt in the NumPy source code)
    Copyright 1999 - 2011 Pearu Peterson all rights reserved.
    https://web.archive.org/web/20140822061353/http://cens.ioc.ee/projects/f2py2e
    """
)

# subparsers = parser.add_subparsers(help="Functional subsets")
build_helpers = parser.add_argument_group("build helpers, only with -c")
generate_wrappers = parser.add_argument_group("wrappers and signature files")

# Common #
##########

# --help is still free
parser.add_argument("--help", action="store_true", help="Print the help")

# TODO: Remove?

parser.add_argument(
    "Fortran Files",
    metavar="<fortran files>",
    action="extend",  # list storage
    nargs="*",
    help="""Paths to fortran/signature files that will be scanned for
                   <fortran functions> in order to determine their signatures.""",
)

parser.add_argument(
    "Skip functions",
    metavar="skip:",
    action="extend",
    type=str,
    nargs="*",
    help="Ignore fortran functions that follow until `:'.",
)

parser.add_argument(
    "Keep functions",
    metavar="only:",
    action="extend",
    type=str,
    nargs="*",
    help="Use only fortran functions that follow until `:'.",
)

parser.add_argument(
    "-m",
    "--module",
    metavar="<modulename>",
    type=str,
    nargs=1,
    help="""Name of the module; f2py generates a Python/C API
                   file <modulename>module.c or extension module <modulename>.
                   Default is 'untitled'.""",
)

parser.add_argument(
    "--lower",
    "--no-lower",
    metavar="--[no-]lower",
    action=BoolAction,
    default=False,
    type=bool,
    help="""Do [not] lower the cases in <fortran files>.
    By default, --lower is assumed with -h key, and --no-lower without -h
    key.""",
)

parser.add_argument(
    "-b",
    "--build-dir",
    metavar="<dirname>",
    type=check_dir,
    nargs=1,
    help="""All f2py generated files are created in <dirname>.
                   Default is tempfile.mkdtemp().""",
)

parser.add_argument(
    "-o",
    "--overwrite-signature",
    action="store_true",
    help="Overwrite existing signature file.",
)

parser.add_argument(
    "--latex-doc",
    "--no-latex-doc",
    metavar="--[no-]latex-doc",
    action=BoolAction,
    type=bool,
    default=False,
    nargs=1,
    help="""Create (or not) <modulename>module.tex.
                   Default is --no-latex-doc.""",
)

parser.add_argument(
    "--short-latex",
    action="store_true",
    help="""Create 'incomplete' LaTeX document (without commands
                   \\documentclass, \\tableofcontents, and \\begin{{document}},
                   \\end{{document}}).""",
)

parser.add_argument(
    "--rest-doc",
    "--no-rest-doc",
    metavar="--[no-]rest-doc",
    action=BoolAction,
    type=bool,
    default=False,
    nargs=1,
    help="""Create (or not) <modulename>module.rst.
                   Default is --no-rest-doc.""",
)

parser.add_argument(
    "--debug-capi",
    dest="debug_api",
    default=[],
    nargs="*",
    action=DebugLinkHelper,
    help="""Create C/API code that reports the state of the wrappers
                   during runtime. Useful for debugging.""",
)

parser.add_argument(
    "--wrap-functions",
    "--no-wrap-functions",
    metavar="--[no-]wrap-functions",
    action=BoolAction,
    type=bool,
    default=True,
    nargs=1,
    help="""Create (or not) Fortran subroutine wrappers to Fortran 77
                   functions. Default is --wrap-functions because it
                   ensures maximum portability/compiler independence""",
)

parser.add_argument(
    "--include-paths",
    nargs='?',
    dest="include_paths",
    action=IncludePathAction,
    metavar="<path1>:<path2>",
    type=str,
    default=[],
    help="Search include files from the given directories.",
)

parser.add_argument(
    "--help-link",
    metavar="..",
    action="extend",
    nargs="*",
    choices=npd_link,
    type=str,
    help="""List system resources found by system_info.py. See also
            --link-<resource> switch below. [..] is optional list
            of resources names. E.g. try 'f2py --help-link lapack_opt'."""
)

parser.add_argument(
    "--f2cmap",
    metavar="<filename>",
    type=pathlib.Path,
    default=".f2py_f2cmap",
    help="""Load Fortran-to-Python KIND specification from the given
                   file. Default: .f2py_f2cmap in current directory.""",
)

parser.add_argument(
    "--quiet",
    action="store_true",
    help="Run quietly.",
)

parser.add_argument(
    "--verbose",
    action="store_true",
    default=True,
    help="Run with extra verbosity.",
)

parser.add_argument(
    "-v",
    action="store_true",
    dest="version",
    help="Print f2py version ID and exit.",
)

# Wrappers/Signatures #
#######################

generate_wrappers.add_argument(
    # TODO: Seriously consider scrapping this naming convention
    "-h",
    "--hint-signature",
    metavar="<filename>",
    type=pathlib.Path,
    nargs=1,
    help="""
    Write signatures of the fortran routines to file <filename> and exit. You
    can then edit <filename> and use it instead of <fortran files>. If
    <filename>==stdout then the signatures are printed to stdout.
    """,
)

# NumPy Distutils #
###################

# TODO: Generalize to allow -c to take other build systems with numpy.distutils
# as a default
build_helpers.add_argument(
    "-c",
    default=False,
    action="store_true",
    help="Compilation (via NumPy distutils)"
)

build_helpers.add_argument(
    "--fcompiler",
    nargs=1,
    type=check_npfcomp,
    help="Specify Fortran compiler type by vendor"
)

build_helpers.add_argument(
    "--compiler",
    nargs=1,
    type=check_dccomp,
    help="Specify distutils C compiler type"
)

build_helpers.add_argument(
    "--help-fcompiler",
    action="store_true",
    help="List available Fortran compilers and exit"
)

build_helpers.add_argument(
    "--f77exec",
    nargs=1,
    type=pathlib.Path,
    help="Specify the path to a F77 compiler"
)

build_helpers.add_argument(
    "--f90exec",
    nargs=1,
    type=pathlib.Path,
    help="Specify the path to a F90 compiler"
)

build_helpers.add_argument(
    "--f77flags",
    nargs=1,
    action=ParseStringFlags,
    help="Specify F77 compiler flags"
)

build_helpers.add_argument(
    "--f90flags",
    nargs=1,
    action=ParseStringFlags,
    help="Specify F90 compiler flags"
)

build_helpers.add_argument(
    "--opt",
    "--optimization_flags",
    nargs=1,
    type=str,
    action=ParseStringFlags,
    help="Specify optimization flags"
)

build_helpers.add_argument(
    "--arch",
    "--architecture_optimizations",
    nargs=1,
    type=str,
    action=ParseStringFlags,
    help="Specify architecture specific optimization flags"
)

build_helpers.add_argument(
    """_summary_
    """    "--noopt",
    action="store_true",
    help="Compile without optimization"
)

build_helpers.add_argument(
    "--noarch",
    action="store_true",
    help="Compile without arch-dependent optimization"
)

build_helpers.add_argument(
    "--debug",
    action="store_true",
    help="Compile with debugging information"
)

build_helpers.add_argument(
    "-L",
    "--library-path",
    type=pathlib.Path,
    metavar="/path/to/lib/",
    nargs=1,
    action="extend",
    default=[],
    help="Path to library"
)

build_helpers.add_argument(
    "-U",
    type=str,
    nargs="*",
    action="extend",
    dest='undef_macros',
    help="Undefined macros"
)

build_helpers.add_argument(
    "-D",
    type=str,
    metavar='MACRO[=value]',
    nargs="*",
    action=ProcessMacros,
    dest="define_macros",
    help="Define macros"
)

build_helpers.add_argument(
    "-l",
    "--library_name",
    type=str,
    metavar="<libname>",
    nargs=1,
    action="extend",
    help="Library name"
)

build_helpers.add_argument(
    "-I",
    "--include_dirs",
    type=pathlib.Path,
    metavar="/path/to/include",
    nargs="*",
    default=[],
    action="extend",
    help="Include directories"
)

# TODO: Kill this ASAP
# Also collect in to REMAINDER and extract from there
# Flag not working. To be debugged.
build_helpers.add_argument(
    '--link-atlas', '--link-atlas_threads', '--link-atlas_blas',
    '--link-atlas_blas_threads', '--link-lapack_atlas',
    '--link-lapack_atlas_threads', '--link-atlas_3_10',
    '--link-atlas_3_10_threads', '--link-atlas_3_10_blas',
    '--link-atlas_3_10_blas_threadslapack_atlas_3_10',
    '--link-lapack_atlas_3_10_threads', '--link-flame', '--link-mkl',
    '--link-openblas', '--link-openblas_lapack', '--link-openblas_clapack',
    '--link-blis', '--link-lapack_mkl', '--link-blas_mkl', '--link-accelerate',
    '--link-openblas64_', '--link-openblas64__lapack', '--link-openblas_ilp64',
    '--link-openblas_ilp64_lapackx11', '--link-fft_opt', '--link-fftw',
    '--link-fftw2', '--link-fftw3', '--link-dfftw', '--link-sfftw',
    '--link-fftw_threads', '--link-dfftw_threads', '--link-sfftw_threads',
    '--link-djbfft', '--link-blas', '--link-lapack', '--link-lapack_src',
    '--link-blas_src', '--link-numpy', '--link-f2py', '--link-Numeric',
    '--link-numeric', '--link-numarray', '--link-numerix', '--link-lapack_opt',
    '--link-lapack_ilp64_opt', '--link-lapack_ilp64_plain_opt',
    '--link-lapack64__opt', '--link-blas_opt', '--link-blas_ilp64_opt',
    '--link-blas_ilp64_plain_opt', '--link-blas64__opt', '--link-boost_python',
    '--link-agg2', '--link-wx', '--link-gdk_pixbuf_xlib_2',
    '--link-gdk-pixbuf-xlib-2.0', '--link-gdk_pixbuf_2', '--link-gdk-pixbuf-2.0',
    '--link-gdk', '--link-gdk_2', '--link-gdk-2.0', '--link-gdk_x11_2',
    '--link-gdk-x11-2.0', '--link-gtkp_x11_2', '--link-gtk+-x11-2.0',
    '--link-gtkp_2', '--link-gtk+-2.0', '--link-xft', '--link-freetype2',
    '--link-umfpack', '--link-amd',
    metavar="--link-<resource>",
    dest="link_resource",
    default=[],
    nargs="*",
    action=NPDLinkHelper,
    help="The link helpers for numpy distutils"
)

parser.add_argument('--backend',
                    type=Backends,
                    default="distutils",
                    action=EnumAction)

# The rest, only works for files, since we expect:
#   <filename>.o <filename>.so <filename>.a
parser.add_argument('otherfiles',
                    type=pathlib.Path,
                    nargs=argparse.REMAINDER)


################
# Main Process #
################

def get_additional_headers(rem: list[str]) -> list[str]:
    return [val[8:] for val in rem if val[:8] == '-include']

def get_f2pyflags_dist(args: argparse.Namespace, skip_funcs: list[str], only_funcs: list[str]) -> list[str]:
    # Distutils requires 'f2py_options' which will be a subset of
    # sys.argv array received. This function reconstructs the array
    # from received args.
    f2py_flags = []
    if(args.wrap_functions):
        f2py_flags.append('--wrap-functions')
    else:
        f2py_flags.append('--no-wrap-functions')
    if(args.lower):
        f2py_flags.append('--lower')
    else:
        f2py_flags.append('--no-lower')
    if(args.debug_api):
        f2py_flags.append('--debug-capi')
    if(args.quiet):
        f2py_flags.append('--quiet')
    f2py_flags.append("--skip-empty-wrappers")
    if(skip_funcs):
        f2py_flags.extend(['skip:']+skip_funcs + [':'])
    if(only_funcs):
        f2py_flags.extend(['only:']+only_funcs + [':'])
    if(args.include_paths):
        f2py_flags.extend(['--include-paths']+[str(include_path) for include_path in args.include_paths])
    if(args.f2cmap):
        f2py_flags.extend(['--f2cmap', str(args.f2cmap)])
    return f2py_flags

def get_fortran_library_flags(args: argparse.Namespace) -> list[str]:
    flib_flags = []
    if args.fcompiler:
        flib_flags.append(f'--fcompiler={args.fcompiler[0]}')
    if args.compiler:
        flib_flags.append(f'--compiler={args.compiler[0]}')
    return flib_flags

def get_fortran_compiler_flags(args: argparse.Namespace) -> list[str]:
    fc_flags = []
    if(args.help_fcompiler):
        fc_flags.append('--help-fcompiler')
    if(args.f77exec):
        fc_flags.append(f'--f77exec={str(args.f77exec[0])}')
    if(args.f90exec):
        fc_flags.append(f'--f90exec={str(args.f90exec[0])}')
    if(args.f77flags):
        fc_flags.append(f'--f77flags={" ".join(args.f77flags)}')
    if(args.f90flags):
        fc_flags.append(f'--f90flags={" ".join(args.f90flags)}')
    if(args.arch):
        fc_flags.append(f'--arch={" ".join(args.arch)}')
    if(args.opt):
        fc_flags.append(f'--opt={" ".join(args.opt)}')
    if(args.noopt):
        fc_flags.append('--noopt')
    if(args.noarch):
        fc_flags.append('--noarch')
    if(args.debug):
        fc_flags.append('--debug')


def get_module_name(args: argparse.Namespace, pyf_files: list[str]) -> str:
    if(args.module is not None):
        return args.module[0]
    if args.c:
        for file in pyf_files:
            if name := get_f2py_modulename(file):
                return name
        return "unititled"
    return None

def get_signature_file(args: argparse.Namespace, build_dir: pathlib.Path) -> pathlib.Path:
    sign_file = None
    if(args.hint_signature):
        sign_file = build_dir /  args.hint_signature[0]
        if sign_file and sign_file.is_file() and not args.overwrite_signature:
            print(f'Signature file "{sign_file}" exists!!! Use --overwrite-signature to overwrite.')
            parser.exit()
    return sign_file

def segregate_posn_args(args: argparse.Namespace) -> tuple[list[str], list[str], list[str]]:
    # Currently, argparse does not recognise 'skip:' and 'only:' as optional args
    # and clubs them all in "Fortran Files" attr. This function segregates them.
    funcs = {"skip:": [], "only:": []}
    mode = "file"
    files = []
    for arg in getattr(args, "Fortran Files"):
        if arg in funcs:
            mode = arg
        elif arg == ':' and mode in funcs:
            mode = "file"
        elif mode == "file":
            files.append(arg)
        else:
            funcs[mode].append(arg)
    return files, funcs['skip:'], funcs['only:']

def process_args(args: argparse.Namespace, rem: list[str]) -> None:
    if args.help:
        parser.print_help()
        parser.exit()
    if(args.version):
        outmess(__version__)
        parser.exit()
    
    # Step 1: Segregate input files from 'skip:' and 'only:' args
    # Read comments in the 'segregate_posn_args' function for more detail
    files, skip_funcs, only_funcs = segregate_posn_args(args)

    # Step 2: Segregate source source files based on their extensions
    f77_files, f90_files, pyf_files, obj_files, other_files = segregate_files(files)

    # Step 3: Open the correct build directory. Read 'open_build_dir' docstring for more detail
    with open_build_dir(args.build_dir, args.c) as build_dir:
        # Step 4: Get module name and signature file path
        module_name = get_module_name(args, pyf_files)
        sign_file = get_signature_file(args, build_dir)

        # Step 5: Parse '-include<header>' flags and store <header>s in a list
        # since argparse can't handle '-include<header>'
        # we filter it out into rem and parse it manually.
        headers = get_additional_headers(rem)
        # TODO: Refine rules settings. Read codebase and remove unused ones

        # Step 6: Generate settings dictionary for f2py internal files
        #         The variables in `rules.py`, `crackfortran.py`, 
        #         `capy_maps.py` and `auxfuncs.py` are set using
        #         information in these dictionaries.
        #         These are the same which 'f2py2e' passes to internal files
        rules_setts = {
            'module': module_name,
            'buildpath': build_dir,
            'dorestdoc': args.rest_doc,
            'dolatexdoc': args.latex_doc,
            'shortlatex': args.short_latex,
            'verbose': args.verbose,
            'do-lower': args.lower,
            'f2cmap_file': args.f2cmap,
            'include_paths': args.include_paths,
            'coutput': None,
            'f2py_wrapper_output': None,
            'emptygen': True,
        }
        crackfortran_setts = {
            'module': module_name,
            'skipfuncs': skip_funcs,
            'onlyfuncs': only_funcs,
            'verbose': args.verbose,
            'include_paths': args.include_paths,
            'do-lower': args.lower,
            'debug': args.debug_api,
            'wrapfuncs': args.wrap_functions,
        }
        capi_maps_setts = {
            'f2cmap': args.f2cmap,
            'headers': headers,
        }
        auxfuncs_setts = {
            'verbose': args.verbose,
            'debug': args.debug_api,
            'wrapfuncs': args.wrap_functions,
        }

        # The function below sets the global and module variables in internal files
        # Read the comments inside this function for explanation
        wrapper_settings(rules_setts, crackfortran_setts, capi_maps_setts, auxfuncs_setts)

		# Step 7: If user has asked for compilation. Mimic 'run_compile' from f2py2e
        # Disutils receives all the options and builds the extension.
        if(args.c and args.backend == Backends.Distutils.value):
            link_resource = args.link_resource

            # The 3 functions below generate arrays of flag similar to how 
            # 'run_compile()' segregates flags into different arrays
            f2py_flags = get_f2pyflags_dist(args, skip_funcs, only_funcs)
            fc_flags = get_fortran_compiler_flags(args)
            flib_flags = get_fortran_library_flags(args)
            
            # The array of flags from above is passed to distutils where 
            # it is handled internally
            ext_args = {
                'name': module_name,
                'sources': pyf_files + f77_files + f90_files,
                'include_dirs': [include_dir.absolute() for include_dir in args.include_dirs],
                'library_dirs': [lib_path.absolute() for lib_path in args.library_path],
                'libraries': args.library_name,
                'define_macros': args.define_macros,
                'undef_macros': args.undef_macros,
                'extra_objects': obj_files,
                'f2py_options': f2py_flags,
            }
            compile_dist(ext_args, link_resource, build_dir, fc_flags, flib_flags, args.quiet)
        else:
            # Step 8: Generate wrapper or signature file if compile flag is not given
            c_wrapper = generate_files(f77_files + f90_files, module_name, sign_file)
            if c_wrapper and args.c:
                backend: Backend = backends.get(args.backend.value)(module_name=module_name, include_dirs=args.include_dirs, include_path=args.include_paths, external_resources=args.link_resource, debug=args.debug)
                backend.compile(f77_files + f90_files, c_wrapper, build_dir)

def main():
    logger = logging.getLogger("f2py_cli")
    logger.setLevel(logging.WARNING)
    args, rem = parser.parse_known_args()
    # since argparse can't handle '-include<header>'
    # we filter it out into rem and parse it manually.
    process_args(args, rem)

if __name__ == "__main__":
    main()
