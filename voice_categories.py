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
    categories = {ROLE_TYPES.get(role, role) for role in facets.get('role', [])}
    genders = {'Male': '男声', 'Female': '女声', 'Neutral': '中性声'}
    age_tags = facets.get('age', [])
    gender_tags = [genders[g] for g in facets.get('gender', []) if g in genders]
    for age in age_tags:
        for gender in gender_tags or ['（性别未标注）']:
            categories.add(age + gender)
    if not categories:
        categories.update(g + '（年龄未标注）' for g in gender_tags)
    facets['role_type'] = sorted(categories) or ['未标注']
    return voice
