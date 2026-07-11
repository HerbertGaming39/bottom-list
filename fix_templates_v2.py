#!/usr/bin/env python3
"""
fix_templates_v2.py - Properly fix broken template url_for references
"""
import re
from pathlib import Path

ROUTE_MAP = {
    'home': 'main',
    'icon_png': 'main',
    'user_profile': 'main',
    'clan_detail': 'main',
    'map_packs': 'main',
    'challenge_list': 'main',
    'platformer_list': 'main',
    'unverified_list': 'main',
    'leaderboard': 'main',
    'clans': 'main',
    'roulette': 'main',
    'events_list': 'main',
    'custom_lists': 'main',
    'forgot_password': 'main',
    'unbeaten_levels': 'main',
    'level_detail': 'main',
    'stats': 'profile',
    'user_stats': 'profile',
    'submit_record': 'profile',
    'submit_level': 'profile',
    'profile': 'profile',
    'edit_profile': 'profile',
    'settings': 'profile',
    'login': 'auth',
    'logout': 'auth',
    'register': 'auth',
    'discord_login': 'auth',
    'discord_link': 'auth',
    'guidelines': 'auth',
    'admin_dashboard': 'admin',
    'admin_shutdown': 'admin',
    'add_level': 'admin',
    'recap': 'recap',
}

def fix_file(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    original = content

    # Pattern: {{ # ) }} or {{ #, ... ) }}
    broken_pattern = r'\{\{\s*#([^}]*)\}\}'

    for match in re.finditer(broken_pattern, content):
        broken = match.group(0)
        suffix = match.group(1).strip()
        start_pos = max(0, match.start() - 200)
        end_pos = min(len(content), match.end() + 50)
        context = content[start_pos:end_pos]

        correct_route = None
        if 'level_id' in suffix or 'level.id' in context:
            correct_route = 'main.level_detail'
        elif 'clan_id' in suffix or 'clan.id' in context:
            correct_route = 'main.clan_detail'
        elif 'user_id' in suffix or 'user.id' in context:
            correct_route = 'main.user_profile'
        elif 'icon' in context.lower() or 'logo' in context.lower():
            correct_route = 'main.icon_png'
        elif 'profile' in context.lower():
            correct_route = 'profile.profile'
        elif 'login' in context.lower():
            correct_route = 'auth.login'
        elif 'register' in context.lower() or 'mode=register' in suffix:
            correct_route = 'auth.register'
        elif 'discord' in context.lower():
            if 'mode=' in suffix:
                correct_route = 'auth.discord_link'
            else:
                correct_route = 'auth.discord_login'
        elif 'clan' in context.lower() and 'clan_id' not in suffix:
            correct_route = 'main.clans'
        elif 'The List' in context:
            correct_route = 'main.home'
        elif 'Challenge' in context:
            correct_route = 'main.challenge_list'
        elif 'Platformer' in context:
            correct_route = 'main.platformer_list'
        elif 'Unverified' in context:
            correct_route = 'main.unverified_list'
        elif 'Leaderboard' in context:
            correct_route = 'main.leaderboard'
        elif 'Submit Record' in context:
            correct_route = 'profile.submit_record'
        elif 'Submit Level' in context:
            correct_route = 'profile.submit_level'
        elif 'Clear' in context or 'Filter' in context:
            correct_route = 'main.home'
        elif 'download' in suffix:
            correct_route = 'admin.admin_dashboard'
        elif 'Back' in context and 'Admin' in context:
            correct_route = 'admin.admin_dashboard'
        elif 'Logout' in context:
            correct_route = 'auth.logout'
        elif 'Settings' in context:
            correct_route = 'profile.settings'
        elif 'Roulette' in context:
            correct_route = 'main.roulette'
        elif 'Events' in context:
            correct_route = 'main.events_list'
        elif 'Map Packs' in context or 'map_pack' in str(filepath).lower():
            correct_route = 'main.map_packs'
        elif 'Custom' in context and 'List' in context:
            correct_route = 'main.custom_lists'
        elif 'Unbeaten' in context:
            correct_route = 'profile.unbeaten_levels'
        elif 'Forgot' in context or 'forgot' in str(filepath).lower():
            correct_route = 'main.forgot_password'
        elif 'Stats' in context:
            correct_route = 'profile.stats'
        elif 'Recap' in context:
            correct_route = 'recap.recap'
        elif 'Add Level' in context or 'add_level' in str(filepath).lower():
            correct_route = 'admin.add_level'
        else:
            fname = Path(filepath).name
            defaults = {
                'login.html': 'auth.login',
                'register.html': 'auth.register',
                'profile.html': 'profile.profile',
                'settings.html': 'profile.settings',
                'edit_profile.html': 'profile.edit_profile',
                'admin.html': 'admin.admin_dashboard',
                'add_level.html': 'admin.admin_dashboard',
                'edit_level.html': 'admin.admin_dashboard',
                'admin_edit_record.html': 'admin.admin_dashboard',
                'admin_db.html': 'admin.admin_dashboard',
                'admin_events.html': 'admin.admin_dashboard',
                'admin_pages.html': 'admin.admin_dashboard',
                'admin_page_edit.html': 'admin.admin_dashboard',
                'admin_map_packs.html': 'admin.admin_dashboard',
                'admin_song_submissions.html': 'admin.admin_dashboard',
                'admin_ui_marketplace.html': 'admin.admin_dashboard',
                'admin_custom_lists.html': 'admin.admin_dashboard',

                'base.html': 'main.home',
                'index.html': 'main.home',
                'level.html': 'main.home',
                'leaderboard.html': 'main.leaderboard',
                'challenge_list.html': 'main.challenge_list',
                'unverified.html': 'main.unverified_list',
                'manage_events.html': 'main.events_list',
                'events.html': 'main.events_list',
                'clans.html': 'main.clans',
                'clan_detail.html': 'main.clans',
                'map_packs.html': 'main.map_packs',
                'custom_lists.html': 'main.custom_lists',
                'discord_oauth_tutorial.html': 'auth.login',
                'forgot_password.html': 'auth.login',
                'guidelines_accept.html': 'auth.logout',
                'guidelines_site.html': 'main.home',
                'unbeaten_recommended.html': 'profile.unbeaten_levels',
                'recap.html': 'profile.profile',
            }
            bp_route = defaults.get(fname, 'main.home')
            correct_route = bp_route

        if correct_route:
            args_part = ''
            if 'level_id' in suffix or 'level_id' in context[:100]:
                args_part = ", level_id=level.id"
            elif 'clan_id' in suffix or 'clan_id' in context[:100]:
                args_part = ", clan_id=clan.id"
            elif 'user_id' in suffix or 'user.id' in context[:100]:
                args_part = ", user_id=user.id"
            new_url = "{{ url_for('" + correct_route + "'" + args_part + ") }}"
            if new_url != broken:
                content = content.replace(broken, new_url, 1)

    if content != original:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        return True
    return False

def main():
    templates_dir = Path(__file__).parent / 'templates'
    total_fixes = 0
    fixed_files = []
    for template_file in sorted(templates_dir.rglob('*.html')):
        if fix_file(template_file):
            rel_path = template_file.relative_to(templates_dir)
            print(f"Fixed: {rel_path}")
            total_fixes += 1
            fixed_files.append(rel_path)
    if total_fixes > 0:
        print(f"\nFixed {total_fixes} template files")
    else:
        print("No broken references found!")

if __name__ == '__main__':
    main()
