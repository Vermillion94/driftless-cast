import pathlib
import py_compile

root = pathlib.Path(__file__).resolve().parent.parent
files = list((root / 'src').rglob('*.py'))
for path in files:
    py_compile.compile(path, doraise=True)
print(f'compiled {len(files)} files')
