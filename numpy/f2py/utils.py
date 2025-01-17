"""Global f2py utilities."""

from __future__ import annotations

import contextlib
import tempfile
import shutil

from typing import Optional
from pathlib import Path

def get_f2py_dir() -> Path:
	"""Return the directory where f2py is installed."""
	return Path(__file__).resolve().parent

@contextlib.contextmanager
def open_build_dir(build_dir: Optional[list[str]], compile: bool) -> Path:
	"""Create build directory if the user specifies it,
 	Otherwise, create a temporary directory and remove it.
  	
	Default build directory for only wrapper generation is
 	the current directory. Therefore, if `compile` is False,
  	the wrappers are generated in the current directory"""

	remove_build_dir: bool = False
	if(isinstance(build_dir, list)):
		build_dir = build_dir[0] if build_dir else None
	if build_dir is None:
		if compile:
			remove_build_dir = True
			build_dir = Path(tempfile.mkdtemp())
		else:
			build_dir = Path.cwd()
	else:
		build_dir = Path(build_dir).absolute()
	yield build_dir
	shutil.rmtree(build_dir) if remove_build_dir else None