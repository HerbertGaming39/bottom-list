import sys
lines = open('/home/marcel/site3/app.py').readlines()
lines[-1] = "    app.run(debug=True, host='0.0.0.0', port=6890, ssl_context=('cert.pem', 'key.pem'))\n"
if '__main__' in lines[-2]:
    lines[-2] = "if __name__ == '__main__':\n"
open('/home/marcel/site3/app.py', 'w').writelines(lines)
