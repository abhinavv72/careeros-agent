"""Reusable answers from the candidate's saved facts, with explicit unit conversion."""
import re
from datetime import date, timedelta


def answer(question, profile, saved):
    q = question.casefold()
    personal = profile.get('personal', {})
    experience = profile.get('experience', {})
    if re.search(r'\b(?:gender|transgender|sexual orientation|race|ethnicity|religion|disability|veteran status|date of birth|social security|aadhaar)\b', q):
        return None
    if re.fullmatch(r'\s*country\W*',q):return personal.get('country')
    if re.search(r'receive communications? via (?:sms|whatsapp)|opt out|reply stop', q): return saved.get('sms_communications_consent')
    if re.search(r'\b(?:phone|mobile|telephone)\b', q) and not re.search(r'country|communication|sms|whatsapp|opt out', q):
        phone_val = personal.get('phone', '')
        digits = re.sub(r'\D', '', phone_val)
        return digits[-10:] if len(digits) >= 10 else digits
    if re.fullmatch(r'\s*location\s*\(?city\)?\W*',q):return ', '.join(str(personal[k]) for k in ('city','state','country') if personal.get(k))
    if 'primarily worked in a product' in q:return saved.get('product_company_experience')
    if 'shipbob' in q and 'ai notetaker' in q and 'consent' in q:return saved.get('shipbob_interview_recording')
    if re.search(r'pricing.*billing.*payments|transaction.based systems',q):return saved.get('transaction_systems_experience')
    if re.search(r'do you have hands.on professional experience with c#/.net',q):return saved.get('dotnet_professional_experience')
    if re.search(r'have you worked with any cloud platforms',q):return saved.get('cloud_platform_experience')
    if re.search(r'family members|relatives|personal relationships?|relationship.*(?:employee|company)|related to an employee',q):
        return saved.get('employee_relationship')
    if 'career goals' in q:return saved.get('why_good_fit')
    if re.search(r'city (?:you )?reside|city of residence', q): return personal.get('city')
    if re.search(r'state (?:you )?reside|state of residence', q): return personal.get('state')
    if re.search(r'legally authorized to work|authorized to work in', q):
        if re.search(r'\b(?:not|never|unable|cannot|can.t|no)\b', q): return None
        region = 'india' if re.search(r'\bindia\b', q) else None
        if re.search(r'\b(?:us|u\.s\.|usa|united states)\b', q): region = 'us'
        elif re.search(r'\bcanada\b', q): region = 'canada'
        elif re.search(r'\b(?:uk|u\.k\.|united kingdom)\b', q): region = 'uk'
        return saved.get(f'work_authorization_{region}') if region else None
    if re.search(r'^(?:what )?country (?:do you )?reside|country of residence', q): return personal.get('country')
    if re.search(r'desired start date|available start date', q):
        if experience.get('noticePeriodDays') is None:return None
        return (date.today() + timedelta(days=int(experience['noticePeriodDays']))).isoformat()
    if re.search(r'(?:desired|expected)(?:.{0,20})salary|expected compensation|salary expectation|compensation expectation', q):
        value = saved.get('expected_compensation')
        if value and re.search(r'\b(?:inr|₹|rupees?|indian rupees?)\b',q):
            match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*lpa\s*', value, re.I)
            if match: return str(int(float(match[1])*100000))
        if value and re.search(r'\b(?:usd|\$|dollars?|eur|€|gbp|£|cad|aud)\b',q):
            saved_currency=re.search(r'\b(?:usd|\$|dollars?|eur|€|gbp|£|cad|aud)\b',str(value),re.I)
            requested_currency=re.search(r'\b(?:usd|\$|dollars?|eur|€|gbp|£|cad|aud)\b',q,re.I)
            if not saved_currency or not requested_currency or saved_currency.group(0).casefold()!=requested_currency.group(0).casefold():return None
        return value
    if re.search(r'current job title|present role|current designation', q): return experience.get('currentTitle')
    if re.search(r'^(?:name of )?(?:current|present) (?:employer|company)|(?:what is your|who is your) (?:current|present) (?:employer|company)', q): return experience.get('currentCompany')
    if re.search(r'preferred (?:first )?name', q): return personal.get('firstName')
    if re.search(r'have you (?:ever )?worked at|previously worked at|employed by.*in the past', q): return saved.get('previous_employer_relationship')
    if re.search(r'outside business activity|side business', q): return saved.get('outside_business_activity')
    if re.search(r'visa sponsorship|immigration sponsorship|need sponsorship|require sponsorship', q): return saved.get('require_sponsorship')
    if re.search(r'source of (?:your )?right to work', q): return saved.get('work_authorization_source')
    if re.search(r'working later hours|overlap with us|able to work this schedule', q): return saved.get('work_schedule_preference')
    if re.search(r'name of (?:your )?(?:current|most recent|present) company|current \(or most recent\) company', q): return experience.get('currentCompany')
    if re.search(r'how did you hear (?:about|of)', q): return saved.get('application_source')
    if re.search(r'candidate privacy policy|privacy policy', q) and re.search(r'agree|consent', q): return 'I agree'
    if re.search(r'current firm', q): return experience.get('currentCompany')
    if re.search(r'less than 2 years of industry experience', q):
        years = saved.get('years_experience', experience.get('years'))
        if years is not None:return 'Yes' if float(years) < 2 else 'No'
        return None
    if re.search(r'previously worked for databricks', q): return 'No'
    return None

