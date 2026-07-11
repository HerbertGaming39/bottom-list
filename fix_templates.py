"""
fix_templates.py - Automatically scan and fix url_for references in templates
"""
import os
import re
from pathlib import Path

# Collect all routes from blueprints
def collect_routes():
    routes = {}
    blueprints_dir = Path(__file__).parent / 'blueprints'
    
    for bp_file in blueprints_dir.glob('*.py'):
        bp_name = bp_file.stem
        with open(bp_file, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Find blueprint variable name
        bp_var_match = re.search(rf"(\w+)_bp\s*=\s*Blueprint\(['\"]{bp_name}['\"]", content)
        if not bp_var_match:
            # Try alternative pattern
            bp_var_match = re.search(rf"(\w+)\s*=\s*Blueprint\(['\"]{bp_name}['\"]", content)
        
        if bp_var_match:
            bp_var = bp_var_match.group(1)
            
            # Find all route definitions
            route_pattern = rf"@{re.escape(bp_var)}\.route\(['\"]([^'\"]+)['\"]"
            for match in re.finditer(route_pattern, content):
                route_path = match.group(1)
                # Extract the function name that follows
                func_match = re.search(rf"def\s+(\w+)\s*\(\)", content[match.end():match.end()+200])
                if func_match:
                    func_name = func_match.group(1)
                    routes[func_name] = bp_name
    
    return routes

# Find all url_for references in templates
def find_url_fors(template_path):
    url_fors = []
    pattern = r"url_for\(['\"]([^'\"]+)['\"]"
    
    with open(template_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    for match in re.finditer(pattern, content):
        full_match = match.group(0)
        endpoint = match.group(1)
        
        # Skip static endpoint
        if endpoint == 'static':
            continue
            
        # Check if already has blueprint prefix
        if '.' in endpoint:
            bp, func = endpoint.split('.', 1)
            url_fors.append({
                'line': content[:match.start()].count('\n') + 1,
                'match': full_match,
                'endpoint': endpoint,
                'has_prefix': True,
                'blueprint': bp,
                'function': func
            })
        else:
            url_fors.append({
                'line': content[:match.start()].count('\n') + 1,
                'match': full_match,
                'endpoint': endpoint,
                'has_prefix': False,
                'blueprint': None,
                'function': endpoint
            })
    
    return url_fors, content

def fix_templates():
    routes = collect_routes()
    templates_dir = Path(__file__).parent / 'templates'
    
    print("=" * 60)
    print("TEMPLATE URL_FOR FIXER")
    print("=" * 60)
    print(f"\nFound {len(routes)} routes across blueprints:")
    for func, bp in sorted(routes.items()):
        print(f"  {func} -> {bp}")
    
    print("\n" + "=" * 60)
    print("SCANNING TEMPLATES...")
    print("=" * 60)
    
    fixes_needed = []
    
    for template_file in templates_dir.rglob('*.html'):
        rel_path = template_file.relative_to(templates_dir)
        url_fors, content = find_url_fors(template_file)
        
        file_fixes = []
        for uf in url_fors:
            func = uf['function']
            
            if uf['has_prefix']:
                # Check if the blueprint prefix is correct
                current_bp = uf['blueprint']
                if func in routes:
                    correct_bp = routes[func]
                    if current_bp != correct_bp:
                        file_fixes.append({
                            'line': uf['line'],
                            'old': uf['match'],
                            'new': f"url_for('{correct_bp}.{func}'",
                            'reason': f"wrong blueprint: {current_bp} -> {correct_bp}"
                        })
                else:
                    # Function doesn't exist anywhere
                    file_fixes.append({
                        'line': uf['line'],
                        'old': uf['match'],
                        'new': None,  # Mark for removal
                        'reason': f"non-existent route: {current_bp}.{func}"
                    })
            else:
                # No prefix - add the correct one
                if func in routes:
                    correct_bp = routes[func]
                    file_fixes.append({
                        'line': uf['line'],
                        'old': uf['match'],
                        'new': f"url_for('{correct_bp}.{func}'",
                        'reason': f"missing prefix: {func} -> {correct_bp}.{func}"
                    })
                else:
                    # Function doesn't exist
                    file_fixes.append({
                        'line': uf['line'],
                        'old': uf['match'],
                        'new': None,
                        'reason': f"non-existent route: {func}"
                    })
        
        if file_fixes:
            fixes_needed.append({
                'file': template_file,
                'rel_path': rel_path,
                'fixes': file_fixes
            })
    
    if not fixes_needed:
        print("\n✅ All templates look good! No fixes needed.")
        return
    
    print(f"\nFound {len(fixes_needed)} files with issues:\n")
    
    for item in fixes_needed:
        print(f"📄 {item['rel_path']}")
        for fix in item['fixes']:
            if fix['new'] is None:
                print(f"   Line {fix['line']}: REMOVE (non-existent: {fix['reason']})")
            else:
                print(f"   Line {fix['line']}: {fix['old']} -> {fix['new']}")
        print()
    
    # Apply fixes
    print("=" * 60)
    print("APPLYING FIXES...")
    print("=" * 60)
    
    for item in fixes_needed:
        template_file = item['file']
        with open(template_file, 'r', encoding='utf-8') as f:
            content = f.read()
        
        original_content = content
        
        for fix in item['fixes']:
            if fix['new'] is None:
                # Remove the entire link tag if it's a simple <a> tag
                # Find and remove the whole <a>...</a> tag containing this url_for
                pattern = rf'<a[^>]*href="\s*{re.escape(fix["old"])}[^"]*"[^>]*>[^<]*</a>\s*'
                content = re.sub(pattern, '', content)
                # Also remove just the url_for if not in an <a> tag
                content = content.replace(fix['old'], "#")
            else:
                content = content.replace(fix['old'], fix['new'], 1)
        
        if content != original_content:
            with open(template_file, 'w', encoding='utf-8') as f:
                f.write(content)
            print(f"✅ Fixed: {item['rel_path']}")
        else:
            print(f"⚠️  No changes: {item['rel_path']}")
    
    print("\n" + "=" * 60)
    print("DONE! Restart your Flask app to test the fixes.")
    print("=" * 60)

if __name__ == '__main__':
    fix_templates()
