"""Global f2py utilities."""

import contextlib
import tempfile
import shutil

from typing import Optional
from pathlib import Path

def get_f2py_dir():
	"""Return the directory where f2py is installed."""
	return Path(__file__).resolve().parent

@contextlib.contextmanager
def open_build_dir(build_dir: Optional[list[Path]], compile: bool):
	remove_build_dir: bool = False
	if(type(build_dir) is list and build_dir[0] is not None):
		build_dir = build_dir[0]
	try:
		if build_dir is None:
			if(compile):
				remove_build_dir = True
				build_dir = tempfile.mkdtemp()
			else:
				build_dir = Path.cwd()
		else:
			build_dir = Path(build_dir).absolute()
		yield build_dir
	finally:
		shutil.rmtree(build_dir) if remove_build_dir else None