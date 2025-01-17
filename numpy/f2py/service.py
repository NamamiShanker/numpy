from __future__ import annotations

import sys
import logging
import re

from pathlib import Path, PurePath
from typing import Any, Optional

# Distutil dependencies
from numpy.distutils.misc_util import dict_append
from numpy.distutils.system_info import get_info
from numpy.distutils.core import setup, Extension

from . import crackfortran
from . import capi_maps
from . import rules
from . import auxfuncs
from . import cfuncs
from . import cb_rules

from .utils import get_f2py_dir

outmess = auxfuncs.outmess

logger = logging.getLogger("f2py_cli")
logger.setLevel(logging.WARNING)

F2PY_MODULE_NAME_MATCH = re.compile(r'\s*python\s*module\s*(?P<name>[\w_]+)',
                                     re.I).match
F2PY_USER_MODULE_NAME_MATCH = re.compile(r'\s*python\s*module\s*(?P<name>[\w_]*?'
                                          r'__user__[\w_]*)', re.I).match

def check_fortran(fname: str) -> Path:
    """Function which checks <fortran files>

    This is meant as a sanity check, but will not raise an error, just a
    warning.  It is called with ``type``

    Parameters
    ----------
    fname : str
        The name of the file

    Returns
    -------
    pathlib.Path
        This is the string as a path, irrespective of the suffix
    """
    fpname = Path(fname)
    if fpname.suffix.lower() not in [".f90", ".f", ".f77"]:
        logger.warning(
            """Does not look like a standard fortran file ending in *.f90, *.f or
            *.f77, continuing against better judgement"""
        )
    return fpname


def check_dir(dname: str) -> Optional[Path]:
    """Function which checks the build directory

    This is meant to ensure no odd directories are passed, it will fail if a
    file is passed. Creates directory if not present.

    Parameters
    ----------
    dname : str
        The name of the directory, by default it will be a temporary one

    Returns
    -------
    pathlib.Path
        This is the string as a path
    """
    if dname:
        dpname = Path(dname)
        dpname.mkdir(parents=True, exist_ok=True)
        return dpname
    return None


def check_dccomp(opt: str) -> str:
    """Function which checks for an np.distutils compliant c compiler

    Meant to enforce sanity checks, note that this just checks against distutils.show_compilers()

    Parameters
    ----------
    opt: str
        The compiler name, must be a distutils option

    Returns
    -------
    str
        This is the option as a string
    """
    cchoices = ["bcpp", "cygwin", "mingw32", "msvc", "unix"]
    if opt in cchoices:
        return opt
    else:
        raise RuntimeError(f"{opt} is not an distutils supported C compiler, choose from {cchoices}")


def check_npfcomp(opt: str) -> str:
    """Function which checks for an np.distutils compliant fortran compiler

    Meant to enforce sanity checks

    Parameters
    ----------
    opt: str
        The compiler name, must be a np.distutils option

    Returns
    -------
    str
        This is the option as a string
    """
    from numpy.distutils import fcompiler
    fcompiler.load_all_fcompiler_classes()
    fchoices = list(fcompiler.fcompiler_class.keys())
    if opt in fchoices[0]:
        return opt
    else:
        raise RuntimeError(f"{opt} is not an np.distutils supported compiler, choose from {fchoices}")


def _set_additional_headers(headers: list[str]) -> None:
    for header in headers:
        cfuncs.outneeds['userincludes'].append(header[1:-1])
        cfuncs.userincludes[header[1:-1]] = f"#include {header}"

def _set_crackfortran(crackfortran_setts: dict[str, Any]) -> None:
    crackfortran.reset_global_f2py_vars()
    crackfortran.f77modulename = crackfortran_setts["module"]
    crackfortran.include_paths[:] = crackfortran_setts["include_paths"]
    crackfortran.debug = crackfortran_setts["debug"]
    crackfortran.verbose = crackfortran_setts["verbose"]
    crackfortran.skipfuncs = crackfortran_setts["skipfuncs"]
    crackfortran.onlyfuncs = crackfortran_setts["onlyfuncs"]
    crackfortran.dolowercase = crackfortran_setts["do-lower"]

def _set_rules(rules_setts: dict[str, Any]) -> None:
    rules.options = rules_setts

def _set_capi_maps(capi_maps_setts: dict[str, Any]) -> None:
    capi_maps.load_f2cmap_file(capi_maps_setts["f2cmap"])
    _set_additional_headers(capi_maps_setts["headers"])

def _set_auxfuncs(aux_funcs_setts: dict[str, Any]) -> None:
    auxfuncs.options = {'verbose': aux_funcs_setts['verbose']}
    auxfuncs.debugoptions = aux_funcs_setts["debug"]
    auxfuncs.wrapfuncs = aux_funcs_setts['wrapfuncs']

def _dict_append(d_out: dict[str, Any], d_in: dict[str, Any]) -> None:
    for (k, v) in d_in.items():
        if k not in d_out:
            d_out[k] = []
        if isinstance(v, list):
            d_out[k] = d_out[k] + v
        else:
            d_out[k].append(v)

def _buildmodules(lst: list[dict[str, Any]]) -> dict[str, Any]:
    cfuncs.buildcfuncs()
    outmess('Building modules...\n')
    modules, mnames = [], []
    isusedby: dict[str, list[Any]] = {}
    for item in lst:
        if '__user__' in item['name']:
            cb_rules.buildcallbacks(item)
        else:
            if 'use' in item:
                for u in item['use'].keys():
                    if u not in isusedby:
                        isusedby[u] = []
                    isusedby[u].append(item['name'])
            modules.append(item)
            mnames.append(item['name'])
    ret: dict[str, Any] = {}
    for module, name in zip(modules, mnames):
        if name in isusedby:
            outmess('\tSkipping module "%s" which is used by %s.\n' % (
                name, ','.join('"%s"' % s for s in isusedby[name])))
        else:
            um = []
            if 'use' in module:
                for u in module['use'].keys():
                    if u in isusedby and u in mnames:
                        um.append(modules[mnames.index(u)])
                    else:
                        outmess(
                            f'\tModule "{name}" uses nonexisting "{u}" '
                            'which will be ignored.\n')
            ret[name] = {}
            _dict_append(ret[name], rules.buildmodule(module, um))
    return ret


def _generate_signature(postlist: list[dict[str, Any]], sign_file: Path) -> None:
    outmess(f"Saving signatures to file {sign_file}" + "\n")
    pyf = crackfortran.crack2fortran(postlist)
    if sign_file in {"-", "stdout"}:
        sys.stdout.write(pyf)
    else:
        with open(sign_file, "w") as f:
            f.write(pyf)

def _check_postlist(postlist: list[dict[str, Any]], sign_file: Path) -> None:
    isusedby: dict[str, list[Any]] = {}
    for plist in postlist:
        if 'use' in plist:
            for u in plist['use'].keys():
                if u not in isusedby:
                    isusedby[u] = []
                isusedby[u].append(plist['name'])
    for plist in postlist:
        if plist['block'] == 'python module' and '__user__' in plist['name'] and plist['name'] in isusedby:
            outmess(
                f'Skipping Makefile build for module "{plist["name"]}" '
                'which is used by {}\n'.format(
                    ','.join(f'"{s}"' for s in isusedby[plist['name']])))
    if(sign_file):
        outmess(
            'Stopping. Edit the signature file and then run f2py on the signature file: ')
        outmess('%s %s\n' %
                (PurePath(sys.argv[0]).name, sign_file))
        return
    for plist in postlist:
        if plist['block'] != 'python module':
            outmess(
                'Tip: If your original code is Fortran source then you must use -m option.\n')

def _callcrackfortran(files: list[str], module_name: str) -> list[dict[str, Any]]:
    postlist = crackfortran.crackfortran([str(file) for file in files])
    for mod in postlist:
        mod["coutput"] = f"{mod['name']}module.c"
        mod["f2py_wrapper_output"] = f"{mod['name']}-f2pywrappers.f"
    return postlist

def _set_dependencies_dist(ext_args: dict[str, Any], link_resource: list[str]) -> None:
    for dep in link_resource:
        info = get_info(dep)
        if not info:
                outmess('No %s resources found in system'
                        ' (try `f2py --help-link`)\n' % (repr(dep)))
        dict_append(ext_args, **info)

def get_f2py_modulename(source: str) -> Optional[str]:
    name = None
    with open(source) as f:
        for line in f:
            if m := F2PY_MODULE_NAME_MATCH(line):
                if F2PY_USER_MODULE_NAME_MATCH(line): # skip *__user__* names
                    continue
                name = m.group('name')
                break
    return name

def wrapper_settings(rules_setts: dict[str, Any], crackfortran_setts: dict[str, Any], capi_maps_setts: dict[str, Any], auxfuncs_setts: dict[str, Any]) -> Optional[Path]:
    # This function also mimics f2py2e. I have added the link to specific code blocks that each function below mimics.
    # Step 6.1: https://github.com/numpy/numpy/blob/45bc13e6d922690eea43b9d807d476e0f243f836/numpy/f2py/f2py2e.py#L331
    _set_rules(rules_setts)
    # Step 6.2: https://github.com/numpy/numpy/blob/main/numpy/f2py/f2py2e.py#L332-L342
    _set_crackfortran(crackfortran_setts)
    # Step 6.3: 1. https://github.com/numpy/numpy/blob/45bc13e6d922690eea43b9d807d476e0f243f836/numpy/f2py/f2py2e.py#L440
    #           2. https://github.com/numpy/numpy/blob/main/numpy/f2py/f2py2e.py#L247-L248
    _set_capi_maps(capi_maps_setts)
    # Step 6.4: 1. https://github.com/numpy/numpy/blob/45bc13e6d922690eea43b9d807d476e0f243f836/numpy/f2py/f2py2e.py#L439
    #           2. https://github.com/numpy/numpy/blob/main/numpy/f2py/f2py2e.py#L471-L473
    _set_auxfuncs(auxfuncs_setts)

def generate_files(files: list[str], module_name: str, sign_file: Path) -> list[Path]:
    """Generate signature file if wanted and return list of wrappers to be compiled"""
    # Step 8.1: Generate postlist from crackfortran
    postlist = _callcrackfortran(files, module_name)

    # Step 8.2: Check postlist. This function is taken from the following code:
    # https://github.com/numpy/numpy/blob/main/numpy/f2py/f2py2e.py#L443-L456
    _check_postlist(postlist, sign_file)
    if(sign_file):
        # Step 8.3: Generate signature file, take from this code piece
        # https://github.com/numpy/numpy/blob/main/numpy/f2py/f2py2e.py#L343-L350
        _generate_signature(postlist, sign_file)
        return
    # Step 8.4: Same as the buildmodules folder of f2py2e
    ret  = _buildmodules(postlist)
    module_name = list(ret.keys())[0]
    wrappers = []
    wrappers.extend(ret.get(module_name).get('csrc', []))
    wrappers.extend(ret.get(module_name).get('fsrc', []))
    return [Path(wrapper) for wrapper in wrappers]

def compile_dist(ext_args: dict[str, Any], link_resources: list[str], build_dir: Path, fc_flags: list[str], flib_flags: list[str], quiet_build: bool) -> None:
    # Step 7.2: The entire code below mimics 'f2py2e:run_compile()'
    # https://github.com/numpy/numpy/blob/main/numpy/f2py/f2py2e.py#L647-L669
    _set_dependencies_dist(ext_args, link_resources)
    f2py_dir = get_f2py_dir()
    ext = Extension(**ext_args)
    f2py_build_flags = ['--quiet'] if quiet_build else ['--verbose']
    f2py_build_flags.extend( ['build', '--build-temp', str(build_dir),
                              '--build-base', str(build_dir),
                              '--build-platlib', '.',
                              '--disable-optimization'])
    if fc_flags:
        f2py_build_flags.extend(['config_fc'] + fc_flags)
    if flib_flags:
        f2py_build_flags.extend(['build_ext'] + flib_flags)

    # f2py2e used to pass `script_name` and `script_args` through `sys.argv` array
    # Now we are passing it as attributes. They will be read later distutils core
    # https://github.com/pypa/distutils/blob/main/distutils/core.py#L131-L134
    setup(ext_modules=[ext], script_name=f2py_dir, script_args=f2py_build_flags)

def segregate_files(files: list[str]) -> tuple[list[str], list[str], list[str], list[str], list[str]]:
	"""
	Segregate files into five groups:
	* Fortran 77 files
	* Fortran 90 and above files
	* F2PY Signature files
	* Object files
	* others
	"""
	f77_ext = ('.f', '.for', '.ftn', '.f77')
	f90_ext = ('.f90', '.f95', '.f03', '.f08')
	pyf_ext = ('.pyf', '.src')
	out_ext = ('.o', '.out', '.so', '.a')

	f77_files = []
	f90_files = []
	out_files = []
	pyf_files = []
	other_files = []

	for f in files:
		f_path = PurePath(f)
		ext = f_path.suffix
		if ext in f77_ext:
			f77_files.append(f)
		elif ext in f90_ext:
			f90_files.append(f)
		elif ext in out_ext:
			out_files.append(f)
		elif ext in pyf_ext:
			if ext == '.src' and f_path.stem.endswith('.pyf') or ext != '.src':
				pyf_files.append(f)
		else:
			other_files.append(f)

	return f77_files, f90_files, pyf_files, out_files, other_files