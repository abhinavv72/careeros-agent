import jobpilot


def test_excluded_role_keyword_blocks_degree_specific_title():
    job = {
        'company': 'Example',
        'title': 'Software Engineer PhD (Freelancer)',
        'description': 'Software engineering work',
        'location': 'Hyderabad',
    }
    eligible, reason = jobpilot.eligible(job, {'excludedRoleKeywords': ['phd']})
    assert not eligible
    assert 'phd' in reason.lower()


def test_excluded_role_keyword_does_not_block_unrelated_role():
    job = {
        'company': 'Example',
        'title': 'Backend Developer',
        'description': 'C# .NET backend work',
        'location': 'Hyderabad',
    }
    assert jobpilot.eligible(job, {'excludedRoleKeywords': ['phd']})[0]
