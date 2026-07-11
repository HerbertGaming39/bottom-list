lines = open('/home/marcel/site3/app.py').readlines()
# Remove all but the last 5 lines, keep the structure clean
# Keep first 371 lines (0-indexed: 0-370), then write the correct final lines
new_lines = lines[:371]
new_lines.append("if __name__ == '__main__':\n")
new_lines.append("    app.run(debug=True, host='0.0.0.0', port=6890, ssl_context=('cert.pem', 'key.pem'))\n")
open('/home/marcel/site3/app.py', 'w').writelines(new_lines)
