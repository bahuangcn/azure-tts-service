"""Common display categories; original provider metadata remains untouched."""
ROLE_TYPES = {
    'Boy': '儿童男声', 'Girl': '儿童女声',
    'YoungAdultMale': '青年男声', 'YoungAdultFemale': '青年女声',
    'OlderAdultMale': '中年男声', 'OlderAdultFemale': '中年女声',
    'SeniorMale': '老年男声', 'SeniorFemale': '老年女声',
    'Narrator': '旁白',
}

def with_categories(voice):
    facets = voice['facets']
    # A shared display field, not a shared vocabulary or a synthesized persona.
    if voice['provider'] == 'azure':
        categories = [ROLE_TYPES.get(role, role) for role in facets.get('role', [])]
        voice['role_type_source'] = 'RolePlayList'
        voice['primary_languages'] = [voice['official']['Locale']] if voice['official'].get('Locale') else []
    else:
        categories = facets.get('age', [])
        voice['role_type_source'] = 'age'
        voice['primary_languages'] = facets.get('language', [])
    facets['role_type'] = sorted(set(categories)) or ['未标注']
    return voice
