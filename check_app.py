import py_compile
try:
    py_compile.compile('/home/marcel/site3/app.py', doraise=True)
    print('OK')
except py_compile.PyCompileError as e:
    print(f'FAIL: {e}')
