"""Validate public API-label exports without importing settings or Discord."""
import re


def validate(data):
    for key, name in data.get('like_topic_symbols', {}).items():
        if not key.isdigit() or not isinstance(name, str) or not re.fullmatch(r'TOPIC_[A-Z0-9_]+', name):
            raise ValueError('Invalid like topic symbol')
    for section in ('school_names', 'school_ranks', 'vote_counts', 'likes', 'school_statuses', 'like_groups'):
        if not isinstance(data.get(section), dict) or not data[section]:
            raise ValueError('Missing API mapping: ' + section)
    for section in ('school_names', 'likes'):
        if any(not key.isdigit() or not isinstance(name, str) or not name for key, name in data[section].items()):
            raise ValueError('Invalid numeric API-label identity')
    if 'boss_names' in data and (not data['boss_names'] or any(
            not key.isdigit() or not isinstance(name, str) or not name
            for key, name in data['boss_names'].items())):
        raise ValueError('Invalid guild boss names')
    for key, row in data['school_statuses'].items():
        if (not re.fullmatch(r'\d+_\d+', key) or key != f"{row.get('school')}_{row.get('identify')}"
                or str(row['school']) not in data['school_names']
                or data['school_ranks'].get(key) != row.get('name_school_identity')):
            raise ValueError('Invalid sect status association')
        value = row.get('vote_time')
        if value is not None and (type(value) is not int or value < 0 or data['vote_counts'].get(key) != value):
            raise ValueError('Invalid sect vote allowance')
    if set(data['school_ranks']) != set(data['school_statuses']):
        raise ValueError('Sect title coverage mismatch')
    expected_votes = {key for key, row in data['school_statuses'].items() if 'vote_time' in row}
    if set(data['vote_counts']) != expected_votes:
        raise ValueError('Sect vote coverage mismatch')
    seen = set()
    for group in data['like_groups'].values():
        if not isinstance(group.get('server_id'), list) or not group['server_id']:
            raise ValueError('Missing server like topics')
        for topic in group['server_id']:
            key = str(topic)
            if type(topic) is not int or topic <= 0 or key in seen or data['likes'].get(key) != group.get('upvote_text'):
                raise ValueError('Invalid like topic association')
            seen.add(key)
    compatibility = data.get('like_compatibility', {})
    if any(key in seen or data['likes'].get(key) != value for key, value in compatibility.items()):
        raise ValueError('Invalid compatibility attribution')
    if set(data['likes']) != seen | set(compatibility):
        raise ValueError('Unattributed like topic')
    return data
