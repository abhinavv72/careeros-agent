"""Evidence-backed screening answers.

``resolve(field, context, kb)`` returns one of three actions:

* ``fill``  – a value plus the evidence refs (live facts, dated records, evidence cards)
  that support it. A fill without evidence is a bug; :func:`resolve` refuses to emit one.
* ``omit``  – leave an optional field empty (unknown, or a "prefer not to say" field
  with no such option).
* ``block`` – a required field cannot be answered truthfully from what is known. The
  reason says what fact or consent would unblock it; the job moves aside.

Nothing here writes answers back. Reusable *question wording* can be learned
(:func:`candidate_kb.learn_template`) separately from personal answers.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from datetime import date, timedelta
import math
import re

import candidate_kb as kb

FILL, OMIT, BLOCK = 'fill', 'omit', 'block'


@dataclass
class Field:
    question: str
    options: list[str] = dc_field(default_factory=list)
    required: bool | None = None
    kind: str = 'text'  # text | textarea | number | choice | checkbox

    @property
    def is_required(self) -> bool:
        if self.required is not None:
            return self.required
        return self.question.rstrip().endswith('*') or bool(re.search(r'\(required\)|\brequired\b', self.question, re.I))


@dataclass
class Context:
    job_id: int | None = None
    employer: str = ''
    job_country: str | None = None   # ISO-3166 alpha-2, None when unknown
    location: str = ''
    source: str = ''
    ats: str = ''
    today: date | None = None


@dataclass
class Resolution:
    action: str
    answer: str | None
    intent: str
    reason: str
    evidence: list[str] = dc_field(default_factory=list)
    needs: str | None = None

    def __bool__(self):
        return self.action == FILL


# ------------------------------------------------------------------ text helpers

def norm(text: str) -> str:
    return kb.norm(re.sub(r'[’‘]', "'", text or ''))


YES_WORDS = re.compile(r"^(?:yes|y|true|i am\b(?!.*\bnot\b)|i do\b(?!.*n.?t)|i have\b(?!.*n.?t)|i agree|agree|i consent|i acknowledge|acknowledged?|i accept|accept|i understand|i certify)", re.I)
NO_WORDS = re.compile(r"^(?:no\b|n\b|false|i am not|i do not|i don.?t|i have not|i haven.?t|not\b|i decline|decline|i do not agree|disagree)", re.I)
PREFER_NOT = re.compile(r"prefer not|decline to|don.?t wish|do not wish|not wish to|rather not|choose not|not to (?:say|disclose|answer|self.?identify|specify)|i don.?t want to answer|undisclosed|not disclosed", re.I)


def pick_bool(options, value: bool) -> str | None:
    if not options:
        return 'Yes' if value else 'No'
    matches = []
    for option in options:
        text = option.strip()
        if PREFER_NOT.search(text):
            continue
        is_no = bool(NO_WORDS.search(text)) or bool(re.search(r"\bnot\b|n't\b", text, re.I))
        is_yes = bool(YES_WORDS.search(text)) and not is_no
        if (value and is_yes) or (not value and is_no):
            matches.append(option)
    return matches[0] if len(matches) == 1 else None


def pick_text(options, target: str) -> str | None:
    if not options:
        return target
    t = norm(target)
    exact = [o for o in options if norm(o) == t]
    if exact:
        return exact[0]
    def within(small, big):
        return len(small) >= 3 and re.search(r'(?<![a-z0-9])' + re.escape(small) + r'(?![a-z0-9])', big)
    contains = [o for o in options if t and (within(t, norm(o)) or within(norm(o), t))]
    return contains[0] if len(contains) == 1 else None


def pick_prefer_not(options) -> str | None:
    hits = [o for o in options if PREFER_NOT.search(o)]
    return hits[0] if hits else None


_RANGE = re.compile(r'(\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)')


def option_range(option: str) -> tuple[float, float] | None:
    text = re.sub(r'\s+', ' ', (option or '').casefold()).strip()
    m = _RANGE.search(text)
    if m:
        return float(m.group(1)), float(m.group(2))
    m = re.search(r'(\d+(?:\.\d+)?)\s*\+|(?:more than|over|above|at least|greater than)\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:years?|yrs?)?\s*(?:or more|and above|plus)', text)
    if m:
        lo = float(next(g for g in m.groups() if g))
        return lo + (0 if '+' in text or 'at least' in text or 'or more' in text or 'and above' in text or 'plus' in text else 1e-9), math.inf
    m = re.search(r'(?:less than|under|below|fewer than)\s*(\d+(?:\.\d+)?)', text)
    if m:
        return 0.0, float(m.group(1)) - 1e-9
    m = re.fullmatch(r'(\d+(?:\.\d+)?)\s*(?:years?|yrs?|days?|months?|lpa|lakhs?|lacs?)?', text)
    if m:
        return float(m.group(1)), float(m.group(1))
    return None


def pick_number(options, value: float, decimals: int = 1) -> str | None:
    if not options:
        factor = 10 ** decimals
        floored = math.floor(value * factor) / factor
        return str(int(floored)) if decimals == 0 or floored == int(floored) else f'{floored:.{decimals}f}'
    exact_integer = [o for o in options if option_range(o) and option_range(o)[0] == option_range(o)[1]]
    if exact_integer and len(exact_integer) == len([o for o in options if option_range(o)]):
        # Discrete choices like 0,1,2,3…: choose the largest not above the true value.
        eligible = [o for o in exact_integer if option_range(o)[0] <= value]
        return max(eligible, key=lambda o: option_range(o)[0]) if eligible else None
    hits = [o for o in options if option_range(o) and option_range(o)[0] <= value <= option_range(o)[1]]
    return hits[0] if len(hits) == 1 else None


# ------------------------------------------------------------------ places

COUNTRIES = {
    'IN': r'india|indian|bharat', 'US': r'united states|\bu\.?s\.?a?\b|\busa\b|america', 'GB': r'united kingdom|\buk\b|u\.k\.|britain|england',
    'CA': r'canada', 'DE': r'germany', 'PL': r'poland', 'SG': r'singapore', 'AU': r'australia', 'IE': r'ireland',
    'NL': r'netherlands', 'FR': r'france', 'ES': r'spain', 'AE': r'united arab emirates|\buae\b|dubai', 'JP': r'japan',
    'BR': r'brazil', 'MX': r'mexico', 'IL': r'israel', 'PH': r'philippines', 'CN': r'china',
}
INDIA_CITIES = r'bangalore|bengaluru|hyderabad|pune|gurgaon|gurugram|noida|delhi|mumbai|chennai|kolkata|ahmedabad|jaipur|kochi|chandigarh|indore|bhubaneswar|trivandrum|coimbatore|mysore|nagpur|lucknow|vadodara|rajaji nagar'
US_HINTS = r'san francisco|new york|seattle|austin|boston|chicago|los angeles|bay area|denver|atlanta|\b(?:ca|ny|wa|tx|ma|il|co|ga|nj|va|or)\b(?:\s*,|\s*$)'


def country_in(text: str) -> str | None:
    t = (text or '').casefold()
    found = [code for code, pattern in COUNTRIES.items() if re.search(pattern, t)]
    if not found:
        if re.search(INDIA_CITIES, t):
            return 'IN'
        if re.search(US_HINTS, t):
            return 'US'
        if re.search(r'london|manchester', t):
            return 'GB'
        if re.search(r'toronto|vancouver|montreal', t):
            return 'CA'
        return None
    return found[0] if len(found) == 1 else None


def job_country(location: str) -> str | None:
    loc = (location or '').casefold()
    if re.search(r'\b(?:worldwide|anywhere|global)\b', loc) and not country_in(loc):
        return None
    return country_in(loc)


def place_in(question: str) -> str | None:
    """The city/region a location question is about."""
    q = question.casefold()
    m = re.search(INDIA_CITIES, q)
    if m:
        return m.group(0)
    m = re.search(r'(?:in|from|to|near|around)\s+(?:the\s+)?([a-z][a-z .]{2,40}?)(?:\s+(?:area|office|region|city))?(?:\?|,|\.|$| for | on | with |\s+this|\s+hybrid)', q)
    return m.group(1).strip() if m else None


# ------------------------------------------------------------------ skills

UNCLAIMED_TECH = ['kubernetes', 'k8s', 'golang', 'ruby', 'rust', 'scala', 'kotlin', 'swift', 'terraform', 'kafka', 'spark', 'airflow',
                  'temporal', 'mlops', 'react native', 'flutter', 'php', 'elixir', 'haskell', 'clojure', 'erlang', 'snowflake', 'databricks',
                  'hadoop', 'elasticsearch', 'redis', 'graphql', 'grpc', 'ansible', 'helm', 'istio', 'jenkins', 'vue', 'svelte', 'next.js',
                  'node.js', 'nodejs', 'django', 'flask', 'rails', 'laravel', 'pytorch', 'tensorflow', 'objective-c', 'perl', 'bash',
                  'powershell', 'cobol', 'sap', 'salesforce', 'servicenow', 'tableau', 'power bi', 'dbt', 'bigquery', 'redshift',
                  'dynamodb', 'cassandra', 'rabbitmq', 'mobile', 'ios', 'android', 'embedded', 'firmware', 'fpga', 'verilog',
                  'cryptography', 'pki', 'static analysis', 'compilers', 'parsers', 'linters', 'sbom', 'open source', 'mlops',
                  'data engineering', 'kernel', 'networking', 'penetration testing', 'threat modeling', 'soc 2', 'iso 27001']


def skills_in(question: str, db) -> list[str]:
    q = ' ' + question.casefold() + ' '
    vocab = set(kb.SKILL_ALIASES) | {kb.canonical_skill(s) for s in kb.claimed_skills(db)} | set(UNCLAIMED_TECH)
    for names in kb.SKILL_ALIASES.values():
        vocab |= names
    vocab.discard('go'); vocab.discard('js'); vocab.discard('ts'); vocab.discard('spring')
    found = []
    for name in sorted(vocab, key=len, reverse=True):
        if len(name) < 2:
            continue
        pattern = r'(?<![a-z0-9+#.])' + re.escape(name) + r'(?![a-z0-9+#])'
        if re.search(pattern, q):
            canon = kb.canonical_skill(name)
            if canon not in found and not any(canon in f and canon != f for f in found):
                found.append(canon)
            q = re.sub(pattern, ' ', q)
    if re.search(r'(?:^|[,/(]|\bin|\bwith|\band|\bor)\s*go\b(?!\s*-?\s*to\b)', q):
        found.append('go')
    return found


# ------------------------------------------------------------------ intents

CONSENT = re.compile(r"\b(?:i consent|consent to|consent|i agree|agree to|i acknowledge|acknowledge|acknowledgement|i certify|certify that|i understand that|i confirm that|i declare|i hereby|terms (?:and|&) conditions|privacy (?:policy|notice|statement)|ccpa|gdpr|pre.?employment statement|by checking|by submitting)\b", re.I)
DEMOGRAPHIC = re.compile(r"\b(?:gender|sex|race|racial|ethnicity|ethnic|hispanic|latin[oax]|veteran|disability|disabled|sexual orientation|pronouns?|transgender|lgbt\w*|religion|caste|marital|date of birth|birth ?date|age range|national origin|neurodivergent)\b", re.I)


def classify(question: str) -> str:
    q = norm(question)
    rules = [
        ('attestation.own_words', r"own words|plagiari[sz]|ai.?generated|use of ai|generated content|without (?:the )?(?:use|help|assistance) of (?:ai|chatgpt|llm)"),
        ('referral', r"referr(?:ed|al) by|employee referral|who referred|referrer|name of (?:the )?(?:employee|person) who"),
        ('marketing', r"receive (?:job )?alerts|similar jobs|stay up to date|newsletter|talent (?:community|network|pool)|marketing|future (?:job )?opportunities|keep (?:me|you) (?:informed|updated)|join our (?:talent|community|mailing|network|newsletter)"),
        ('consent', None),
        ('demographic', None),
        ('option_label', r"^(?:less than \d+ years?|more than \d+ years?|\d+\+? years?|limited (?:exposure|experience)|no (?:professional )?experience|some experience|extensive experience|basic|advanced|expert|beginner|intermediate)\b.{0,40}?\bi (?:have|am|explored|do)\b"),
        ('opinion', r"in your opinion|what are the (?:\w+ )?(?:most important|key|biggest)|what do you think|how would you (?:approach|design|handle|improve)|what would you (?:do|change|build)"),
        ('free_text.lead', r"^(?:please )?(?:list|describe|tell us|explain|share|walk us through|give (?:us )?(?:an )?example|elaborate)\b"),
        ('how_heard', r"how did you (?:first )?(?:hear|learn|find|come across)|how you heard|where did you (?:hear|find|see|learn)|source of (?:application|referral|this)|hear about (?:us|this)|learn about (?:us|this)"),
        ('sponsorship', r"sponsor|visa|immigration|h ?1 ?b|work permit support"),
        ('citizenship', r"citizen(?:ship)?|nationality"),
        ('work_authorization', r"authori[sz]ed to work|authori[sz]ation to work|eligib(?:le|ility) to work|right to work|work authori[sz]ation|permitted to work|legally (?:able|allowed) to work|work rights"),
        ('prior_employment', r"(?:have|had) you (?:ever )?(?:previously )?(?:worked|been employed|been an employee)|(?:are|is|were) you (?:currently )?(?:working|employed) (?:at|for|with|by)|previously (?:worked|employed)|former employee|employed by .{0,60}(?:before|past|previously)|worked (?:at|for) .{0,60}(?:before|past|previously)|ex.?employee"),
        ('relationship', r"family member|relatives?|related to (?:an|any) (?:employee|member)|personal relationship|relationship with (?:an|any)"),
        ('outside_business', r"outside business|side business|advisory|board (?:role|member)|other employment|moonlight"),
        ('non_compete', r"non.?compet|restrictive covenant|non.?solicit|agreement that would (?:preclude|restrict)"),
        ('criminal', r"felony|criminal|convicted|pleaded guilty|misdemeanou?r"),
        ('age', r"(?:18|eighteen) years|age of (?:18|majority)|over (?:the age of )?18|legal age|at least 18"),
        ('notice_period', r"notice period|how soon can you|joining (?:date|time)|immediate(?:ly)? join|immediate joiner|join (?:us )?immediately|start date|available to start|availability to (?:join|start)|when can you (?:start|join)|earliest (?:start|joining)|serving notice"),
        ('compensation', r"\bctc\b|salary|compensation|pay (?:expectation|range)|\blpa\b|remuneration|package"),
        ('relocation', r"relocat|willing to (?:work|travel|commute|be based)|comfortable (?:to |with |in )?(?:work|working|commut|travel)|work from (?:the )?office|hybrid|on.?site|in.?office|present in|work (?:out of|from) our|able to (?:work|commute) (?:from|in|to)|travel to"),
        ('timezone', r"time ?zone"),
        ('english', r"english|language proficiency|proficiency level"),
        ('education.grade', r"\b(?:cgpa|gpa|grade point|percentage in|marks|degree result|(?:university|college|final) result|classification)\b"),
        ('education.institution', r"\b(?:university|college|school|institution|alma mater)\b(?!.*\bhigh school\b)"),
        ('education.degree', r"\b(?:degree|qualification|highest education|major|field of study|discipline)\b"),
        ('education.year', r"graduat(?:ion|ed) (?:year|date)|year of (?:graduation|passing)|when did you graduate"),
        ('identity.first_name', r"^(?:legal )?first name|\bfirst name\b|\bgiven name\b"),
        ('identity.last_name', r"\blast name\b|\bsurname\b|\bfamily name\b"),
        ('identity.preferred_name', r"preferred (?:first )?name|nickname"),
        ('identity.full_name', r"^(?:full |legal |your )?(?:legal )?name\b|full (?:legal )?name|^name\W*$"),
        ('identity.email', r"\be.?mail\b"),
        ('identity.phone_code', r"country (?:phone |dial(?:ing)? |calling )?code|phone code|dial(?:ing)? code|calling code"),
        ('identity.phone', r"\b(?:phone|mobile|telephone|cell|contact number|whatsapp number)\b"),
        ('identity.linkedin', r"linked ?in"),
        ('identity.github', r"git ?hub"),
        ('identity.portfolio', r"portfolio|personal (?:website|site|url)|website (?:url|link|address)|your (?:website|site)|^website\W*$"),
        ('identity.postal_code', r"postal|zip|pin ?code"),
        ('identity.address', r"address line|street address|^address"),
        ('current_employer', r"current (?:or most recent )?(?:employer|company|organi[sz]ation|firm)|(?:current|most recent|present) (?:\(or most recent\) )?company|name of (?:your )?(?:current|most recent|present) (?:company|employer)|present firm|where do you (?:currently )?work"),
        ('current_title', r"current (?:job )?(?:title|designation|role|position)|present (?:role|designation)|job title"),
        ('experience.threshold', r"(?:less|more|fewer) than \d+ years? of (?:industry |professional |work )?experience"),
        ('experience.years', r"how many years|number of years|years of (?:\w+ ){0,4}experience|years experience|experience in years|total experience|overall experience|experience \(?(?:in )?years|yrs? of exp|work experience|professional experience|industry experience"),
        ('skill.yes_no', r"(?:do|did) you have (?:any )?(?:\w+ )?(?:experience|exposure|knowledge|familiarity)|have you (?:ever )?(?:worked|used|built|developed|shipped|deployed|written)|are you (?:experienced|proficient|familiar|comfortable)|experience (?:with|in|using)|hands.on|proficien|familiar(?:ity)? with|knowledge of|worked with"),
        ('residence', r"(?:currently )?(?:located|living|live|reside|residing|based) (?:in|near|around)|are you (?:currently )?in\b|do you (?:currently )?live|where are you (?:currently )?(?:located|based)|current (?:location|city)|city (?:of residence|you (?:live|reside))|country (?:of residence|you (?:live|reside))|state (?:of residence|you (?:live|reside))|^location|^city\W*$|^country\W*$|^state\W*$"),
        ('free_text', r"\b(?:describe|tell us|explain|share|walk us through|give (?:us )?(?:an )?example|why|what (?:interests|excites|motivates|attracts|draws)|what (?:is|was) (?:the|a|your) (?:most|biggest|hardest)|summari[sz]e|elaborate|how would you|what would you|what have you|what security|what interactions|which (?:programming )?languages)\b"),
    ]
    short_label = len(q.split()) <= 12
    for intent, pattern in rules:
        if intent.startswith('identity.') and not short_label:
            continue
        if intent == 'consent':
            if CONSENT.search(q) and not re.search(r'\bdo you consent to (?:relocat|travel)', q):
                return 'consent.' + consent_type(q)
            continue
        if intent == 'demographic':
            if DEMOGRAPHIC.search(q) and not re.search(r'experience|years|salary', q):
                return 'demographic'
            continue
        if re.search(pattern, q):
            return intent
    return 'unknown'


LEGAL_WAIVER = re.compile(r"\bwaiv(?:e|er|ing)\b|release (?:all |any )?(?:claims|liability)|arbitrat|class action|indemnif|hold harmless|non.?disparag|covenant not to sue", re.I)


def consent_type(q: str) -> str:
    for name, pattern in (('sms', r'\bsms\b|whats ?app|text messag'), ('recording', r'record|notetaker|note taker|transcri'),
                          ('demographic_processing', r'demographic|survey|self.?identif|voluntary'),
                          ('background_check', r'background (?:check|verification|screening)|reference|verify (?:the )?information|drug test'),
                          ('truthful_certification', r'certify|true and complete|complete and accurate|accurate and complete|truthful|declare|pre.?employment statement|false information'),
                          ('privacy_notice', r'privacy|ccpa|gdpr|personal (?:data|information)|data (?:protection|processing)'),
                          ('marketing', r'marketing|newsletter|promotional|alerts')):
        if re.search(pattern, q):
            return name
    return 'terms'


# ------------------------------------------------------------------ resolver

def ctx_country(r) -> str | None:
    return getattr(r, 'ctx', None) and r.ctx.job_country


class _R:
    """Small builder that enforces 'no fill without evidence'."""

    def __init__(self, field: Field, intent: str, ctx: 'Context | None' = None):
        self.field, self.intent, self.ctx = field, intent, ctx

    def fill(self, answer, evidence, reason=''):
        evidence = [e for e in evidence if e]
        if answer is None or str(answer).strip() == '':
            return self.unknown(reason or 'Value could not be formatted for this field')
        if not evidence:
            raise AssertionError(f'fill without evidence for {self.intent}')
        return Resolution(FILL, str(answer), self.intent, reason or 'Supported by saved facts', evidence)

    def unknown(self, reason, needs=None):
        action = BLOCK if self.field.is_required else OMIT
        return Resolution(action, None, self.intent, reason, needs=needs)

    def options_mismatch(self, value, evidence):
        return self.unknown(f'Known value "{value}" does not match any offered option', needs='form review')


def _fact(db, key, scope='global'):
    return kb.get_fact(db, key, scope)


def _bool(value) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value).strip().casefold()
    if text in ('yes', 'y', 'true', '1', 'granted', 'i agree', 'agree'):
        return True
    if text in ('no', 'n', 'false', '0', 'denied', 'none'):
        return False
    return None


def _bool_answer(r: _R, value: bool, evidence, reason=''):
    choice = pick_bool(r.field.options, value)
    if choice is None:
        return r.options_mismatch('Yes' if value else 'No', evidence)
    return r.fill(choice, evidence, reason)


def _residence_country(db) -> tuple[str | None, list[str]]:
    f = _fact(db, 'personal.country')
    return (country_in(f.value), [f.ref]) if f else (None, [])


def resolve(field: Field, ctx: Context | None = None, db=None) -> Resolution:
    ctx = ctx or Context()
    own_db = db is None
    db = db or kb.connect()
    try:
        return _resolve(field, ctx, db)
    finally:
        if own_db:
            db.close()


def _resolve(field: Field, ctx: Context, db) -> Resolution:
    q_raw = field.question or ''
    q = norm(q_raw)
    today = ctx.today or date.today()
    if not q:
        return Resolution(OMIT, None, 'empty', 'No question text')

    exact = _fact(db, 'answer.question:' + q[:200])
    if exact:
        r = _R(field, 'saved_exact')
        choice = pick_text(field.options, str(exact.value)) if field.options else str(exact.value)
        return r.fill(choice, [exact.ref], 'Candidate saved this exact answer') if choice else r.options_mismatch(exact.value, [exact.ref])

    for pattern, key in SAVED_FREE_TEXT:
        if re.search(pattern, q):
            fact = _fact(db, key)
            if fact and not field.options:
                return _R(field, 'saved_answer').fill(str(fact.value), [fact.ref], 'Candidate saved this answer')

    template = kb.template_for(db, q_raw)
    intent = template['intent'] if template else classify(q_raw)
    r = _R(field, intent, ctx)

    if intent == 'option_label':
        return r.unknown('This text is an answer option, not a question; the real question was not captured', needs='form review')
    if intent == 'opinion':
        return r.unknown('Opinion question needs the candidate\'s own answer', needs='candidate answer')

    if intent == 'attestation.own_words':
        return Resolution(BLOCK, None, intent, 'The form asks the candidate to attest that answers are their own words; an automated answer would make that attestation false',
                          needs='candidate writes these answers personally')

    if intent == 'demographic':
        choice = pick_prefer_not(field.options)
        if choice:
            return r.fill(choice, ['policy:voluntary-self-identification-declined'], 'Voluntary self-identification declined')
        return r.unknown('Demographic question without a decline option; no saved answer', needs='candidate answers personally')

    if intent == 'marketing' or intent == 'consent.marketing':
        if field.options:
            choice = pick_bool(field.options, False)
            if choice:
                return r.fill(choice, ['policy:decline-optional-marketing'], 'Optional marketing declined')
        return Resolution(OMIT, None, intent, 'Optional marketing opt-in left unchecked') if not field.is_required else \
            r.unknown('Required marketing consent', needs='consent.marketing')

    if intent.startswith('consent.'):
        kind = intent.split('.', 1)[1]
        if LEGAL_WAIVER.search(q):
            return Resolution(BLOCK, None, intent, 'Consent text waives legal rights (claims, arbitration or indemnity); '
                              'standing consents never cover this', needs='candidate reviews this employer statement')
        grant = _fact(db, f'consent.{kind}')
        if grant and _bool(grant.value):
            if field.kind == 'checkbox':
                return r.fill('Yes', [grant.ref], f'Standing consent: {kind}')
            choice = pick_bool(field.options, True) if field.options else 'Yes'
            if not choice and field.options:
                choice = next((o for o in field.options if re.search(r'consent|agree|acknowledge|accept|understand|confirm', o, re.I)
                               and not re.search(r'\bnot\b|n.t\b|disagree|decline', o, re.I)), None)
            return r.fill(choice, [grant.ref], f'Standing consent: {kind}') if choice else r.options_mismatch('consent', [grant.ref])
        if grant and _bool(grant.value) is False and field.options:
            choice = pick_bool(field.options, False)
            if choice:
                return r.fill(choice, [grant.ref], f'Candidate declined: {kind}')
        return r.unknown(f'No standing consent for "{kind}"', needs=f'consent.{kind}')

    if intent == 'referral':
        return r.unknown('No referral is recorded', needs='referral contact')

    if intent == 'how_heard':
        saved = _fact(db, 'preference.application_source')
        if saved:
            choice = pick_text(field.options, str(saved.value))
            return r.fill(choice, [saved.ref]) if choice else r.options_mismatch(saved.value, [saved.ref])
        source = (ctx.source or '').casefold()
        ats = (ctx.ats or '').casefold()
        labels = []
        if 'linkedin' in source:
            labels = ['LinkedIn']
        elif any(s in source for s in ('naukri', 'indeed', 'himalayas', 'remotive', 'wellfound', 'instahyre', 'hirist', 'glassdoor')):
            name = next(s for s in ('naukri', 'indeed', 'himalayas', 'remotive', 'wellfound', 'instahyre', 'hirist', 'glassdoor') if s in source)
            labels = [name.title(), 'Job board', 'Job site', 'Online job board', 'Other']
        elif source or ats in ('greenhouse', 'lever', 'ashby', 'workday', 'smartrecruiters'):
            labels = ['Company website', 'Careers website', 'Career site', 'Careers page', 'Company careers page', 'Website', 'Job board', 'Other']
        for label in labels:
            choice = pick_text(field.options, label) if field.options else label
            if choice:
                return r.fill(choice, [f'job:{ctx.job_id}:source={ctx.source or ats}'], 'From the discovery record for this listing')
        return r.unknown('Listing source unknown or not among the options', needs='application source')

    if intent in ('work_authorization', 'sponsorship'):
        country = country_in(q)
        residence, res_ev = _residence_country(db)
        if re.search(r'country (?:where|in which) (?:this|the) (?:job|role|position)|(?:job|role|position) is located|this location', q):
            country = ctx.job_country
        elif re.search(r'country (?:you|in which you|where you) (?:currently )?(?:reside|live)|your (?:current )?country', q):
            country = residence
        elif country is None:
            country = ctx.job_country
        if not country:
            return r.unknown('Country for this question is unknown (job location not specific)', needs='job country')
        key = 'work_authorization' if intent == 'work_authorization' else 'require_sponsorship'
        fact = _fact(db, key, f'country:{country}')
        if not fact or _bool(fact.value) is None:
            return r.unknown(f'No saved {key.replace("_", " ")} for {country}', needs=f'{key}@country:{country}')
        value = _bool(fact.value)
        if intent == 'work_authorization' and re.search(r'\b(?:not|unable|cannot|can t|never)\b', q):
            value = not value
        if intent == 'sponsorship' and re.search(r'\bnot\b.{0,20}\brequire|\bwithout\b', q):
            value = not value
        if intent == 'work_authorization' and value and field.options and pick_bool(field.options, True) is None:
            unrestricted = [o for o in field.options if re.search(r'\bunrestricted\b', o, re.I) and not re.search(r'\bnot\b|do not', o, re.I)]
            if len(unrestricted) == 1:
                return r.fill(unrestricted[0], [fact.ref] + res_ev, f'Saved {key} for {country}')
        return _bool_answer(r, value, [fact.ref] + res_ev, f'Saved {key} for {country}')

    if intent == 'citizenship':
        fact = _fact(db, 'personal.citizenship')
        if not fact:
            return r.unknown('Citizenship is not recorded', needs='personal.citizenship')
        named = country_in(q)
        if named and re.search(r'^(?:are|is) you|citizen\b', q) and field.options and len(field.options) <= 3:
            return _bool_answer(r, country_in(str(fact.value)) == named, [fact.ref])
        choice = pick_text(field.options, str(fact.value))
        return r.fill(choice, [fact.ref]) if choice else r.options_mismatch(fact.value, [fact.ref])

    if intent == 'prior_employment':
        complete = _fact(db, 'employment.history_complete')
        if not complete or not complete.value:
            return r.unknown('Employment history is not marked complete', needs='employment.history_complete')
        target = ctx.employer
        head = next((w for w in norm(target).split() if len(w) >= 4), '')
        if not target or not (norm(target) in q or (head and re.search(r'(?<![a-z0-9])' + re.escape(head), q)) or re.search(r'\b(?:us|this company|our company|subsidiar|affiliat|in the past|before|previously)\b', q)):
            return r.unknown('Could not tell which employer the question refers to', needs='form review')
        history = kb.employment(db)
        t = re.sub(r'[^a-z0-9]', '', target.casefold())
        hit = [e for e in history if any(t and (t in re.sub(r'[^a-z0-9]', '', n.casefold()) or re.sub(r'[^a-z0-9]', '', n.casefold()) in t)
                                         for n in [e['employer'], *e['aliases']])]
        return _bool_answer(r, bool(hit), [complete.ref] + [f"employment:{e['id']}" for e in history], 'Compared with dated employment history')

    if intent in ('relationship', 'outside_business', 'non_compete', 'criminal', 'age'):
        key = {'relationship': 'relationship_with_employee', 'outside_business': 'outside_business_activity',
               'non_compete': 'non_compete_restriction', 'criminal': 'criminal_record_disclosure', 'age': 'age_18_or_older'}[intent]
        fact = _fact(db, key)
        if not fact or _bool(fact.value) is None:
            return r.unknown(f'No saved answer for {key}', needs=key)
        return _bool_answer(r, _bool(fact.value), [fact.ref])

    if intent == 'notice_period':
        fact = _fact(db, 'notice_period_days')
        if not fact:
            return r.unknown('Notice period is not recorded', needs='notice_period_days')
        days = int(fact.value)
        if re.search(r'immediate|join (?:us )?immediately', q):
            return _bool_answer(r, days == 0, [fact.ref])
        if re.search(r'serving (?:your )?notice', q):
            return r.unknown('Whether notice is being served is not recorded', needs='notice.serving')
        if re.search(r'start date|earliest|available to start|when can you', q) and not re.search(r'notice', q):
            if field.options:
                return r.unknown('Start date options need a calendar choice', needs='form review')
            return r.fill((today + timedelta(days=days)).isoformat(), [fact.ref], 'Today plus saved notice period')
        if field.options:
            choice = pick_number(field.options, days, 0)
            if not choice:
                months = days / 30
                choice = next((o for o in field.options if re.search(rf'\b{int(months)}\s*months?\b', o, re.I) and months == int(months)), None)
            return r.fill(choice, [fact.ref]) if choice else r.options_mismatch(f'{days} days', [fact.ref])
        if field.kind == 'number' or re.search(r'in days|number of days|\(days\)', q):
            return r.fill(str(days), [fact.ref])
        if re.search(r'in months|\(months\)', q):
            return r.fill(str(round(days / 30, 1)).rstrip('0').rstrip('.'), [fact.ref])
        return r.fill(f'{days} days', [fact.ref])

    if intent == 'compensation':
        return _compensation(r, field, q, db)

    if intent == 'relocation':
        place = place_in(q_raw)
        prefs = _fact(db, 'preference.locations')
        city = _fact(db, 'personal.city')
        if place and prefs and any(norm(place) in norm(p) or norm(p) in norm(place) for p in prefs.value):
            return _bool_answer(r, True, [prefs.ref], f'{place} is a saved preferred location')
        if place and city and norm(place) == norm(city.value):
            return _bool_answer(r, True, [city.ref])
        if re.search(r'relocat', q):
            fact = _fact(db, 'preference.willing_to_relocate')
            if fact and _bool(fact.value) is not None:
                return _bool_answer(r, _bool(fact.value), [fact.ref])
        return r.unknown(f'No saved preference for working from {place or "this location"}', needs='preference.locations')

    if intent == 'residence':
        return _residence(r, field, q, q_raw, db)

    if intent == 'timezone':
        country, ev = _residence_country(db)
        if country == 'IN':
            for label in ('IST', 'India Standard Time', 'UTC+5:30', 'GMT+5:30', 'UTC +5:30', 'Asia/Kolkata', 'Asia', 'India'):
                choice = pick_text(field.options, label) if field.options else 'India Standard Time (UTC+5:30)'
                if choice:
                    return r.fill(choice, ev, 'Derived from country of residence')
        return r.unknown('Time zone not derivable', needs='personal.timezone')

    if intent == 'english':
        fact = _fact(db, 'language.english')
        if not fact:
            return r.unknown('English proficiency is not recorded', needs='language.english')
        choice = pick_text(field.options, str(fact.value))
        return r.fill(choice, [fact.ref]) if choice else r.options_mismatch(fact.value, [fact.ref])

    if intent.startswith('education.'):
        edu = kb.education(db)
        if len(edu) != 1:
            return r.unknown('Education record missing or ambiguous', needs='education')
        e = edu[0]
        value = {'education.grade': (e['grade'] or '').replace('CGPA ', ''), 'education.institution': e['institution'],
                 'education.degree': e['degree'], 'education.year': (e['end'] or '')[:4]}[intent]
        if intent == 'education.grade' and re.search(r'percent|%', q):
            return r.unknown('Grade is recorded as CGPA, not a percentage', needs='education.percentage')
        if intent == 'education.grade' and field.kind == 'number':
            value = value.split('/')[0]
        choice = pick_text(field.options, value) if field.options else value
        return r.fill(choice, [f"education:{e['id']}"]) if choice else r.options_mismatch(value, [f"education:{e['id']}"])

    if intent.startswith('identity.'):
        return _identity(r, field, intent, db)

    if intent in ('current_employer', 'current_title'):
        current = [e for e in kb.employment(db) if not e['end']]
        if len(current) != 1:
            return r.unknown('No single current employment record', needs='employment')
        value = current[0]['employer'] if intent == 'current_employer' else current[0]['title']
        choice = pick_text(field.options, value) if field.options else value
        return r.fill(choice, [f"employment:{current[0]['id']}"]) if choice else r.options_mismatch(value, [])

    if intent in ('experience.years', 'experience.threshold'):
        return _experience(r, field, q, q_raw, db, today)

    if intent == 'skill.yes_no':
        skills = skills_in(q_raw, db)
        if not skills:
            return r.unknown('Could not identify the skill being asked about', needs='form review')
        claimed = kb.claimed_skills(db)
        professional = bool(re.search(r'professional|production|commercial|industry|work experience|on the job', q))
        either = bool(re.search(r'\bor\b|/|any of', q))
        supported = []
        for s in skills:
            if professional:
                hits = [c['id'] for c in kb.cards(db) if c['kind'] == 'employment' and s in {kb.canonical_skill(x) for x in c['skills']}]
            else:
                claimed_fact = _fact(db, 'skills.claimed')
                hits = ([claimed_fact.ref] if claimed_fact and s in {kb.canonical_skill(x) for x in claimed_fact.value} else []) or                        [f"card:{c['id']}" for c in kb.cards(db) if s in {kb.canonical_skill(x) for x in c['skills']}]
            supported.append((s, hits))
        ok = [s for s, h in supported if h]
        if (either and ok) or (not either and len(ok) == len(skills)):
            evidence = sorted({e for _, h in supported for e in h})
            return _bool_answer(r, True, evidence, 'Skill appears in résumé evidence' + (' from employment' if professional else ''))
        missing = [s for s, h in supported if not h]
        return r.unknown(f'No evidence for {", ".join(missing)}' + (' in professional work' if professional else ''),
                         needs='skill fact: ' + ', '.join(missing))

    if intent in ('free_text', 'free_text.lead') or (intent == 'unknown' and field.kind == 'textarea'):
        return _free_text(r, field, q, q_raw, db, ctx)

    return r.unknown('No rule or saved fact answers this question', needs='candidate answer')


def _identity(r, field, intent, db):
    key = {'identity.first_name': 'firstName', 'identity.last_name': 'lastName', 'identity.preferred_name': 'firstName',
           'identity.email': 'email', 'identity.phone': 'phone', 'identity.phone_code': 'phone', 'identity.linkedin': 'linkedin',
           'identity.github': 'github', 'identity.portfolio': 'portfolio', 'identity.postal_code': 'postalCode',
           'identity.full_name': None, 'identity.address': 'address'}[intent]
    if intent == 'identity.full_name':
        first, last = _fact(db, 'personal.firstName'), _fact(db, 'personal.lastName')
        if not (first and last):
            return r.unknown('Name not recorded', needs='personal.firstName/lastName')
        return r.fill(f'{first.value} {last.value}', [first.ref, last.ref])
    fact = _fact(db, f'personal.{key}')
    if not fact:
        return r.unknown(f'{key} is not recorded', needs=f'personal.{key}')
    value = str(fact.value)
    if intent == 'identity.phone_code':
        m = re.match(r'\s*\+(\d{1,3})', value)
        if not m:
            return r.unknown('Phone number has no country code', needs='personal.phone')
        code = '+' + m.group(1)
        if field.options:
            choice = next((o for o in field.options if re.search(re.escape(code) + r'(?!\d)', o)), None)
            return r.fill(choice, [fact.ref]) if choice else r.options_mismatch(code, [fact.ref])
        return r.fill(code, [fact.ref])
    if intent == 'identity.phone' and (field.kind == 'number' or re.search(r'without (?:country )?code|10.digit', norm(field.question))):
        digits = re.sub(r'\D', '', value)
        value = digits[-10:]
    choice = pick_text(field.options, value) if field.options else value
    return r.fill(choice, [fact.ref]) if choice else r.options_mismatch(value, [fact.ref])


def _residence(r, field, q, q_raw, db):
    city, state, country = (_fact(db, f'personal.{k}') for k in ('city', 'state', 'country'))
    yes_no = bool(re.search(r'^(?:are|do|is) you|^are you|currently (?:in|located|living|based)', q)) and not re.search(r'^where|which|what', q)
    if yes_no or (field.options and all(pick_bool([o], True) or pick_bool([o], False) for o in field.options)):
        place = place_in(q_raw)
        named_country = country_in(q)
        if named_country and country and not re.search(INDIA_CITIES, q):
            return _bool_answer(r, country_in(country.value) == named_country, [country.ref], 'Compared with country of residence')
        if place and city:
            same = norm(place) in norm(city.value) or norm(city.value) in norm(place) or (state and norm(place) == norm(state.value))
            return _bool_answer(r, bool(same), [city.ref] + ([state.ref] if state else []), 'Compared with current city')
        return r.unknown('Could not tell which place the question refers to', needs='form review')
    if re.search(r'country', q) and country:
        choice = pick_text(field.options, country.value) if field.options else country.value
        return r.fill(choice, [country.ref]) if choice else r.options_mismatch(country.value, [country.ref])
    if re.search(r'\bstate\b', q) and state:
        choice = pick_text(field.options, state.value) if field.options else state.value
        if not choice and field.options and country and country_in(str(country.value)) != 'US' and len(field.options) > 5:
            choice = next((o for o in field.options if norm(o) in ('n a', 'na', 'n/a', 'not applicable', 'none', 'outside us', 'outside the us', 'international')), None)
            if choice:
                return r.fill(choice, [state.ref, country.ref], 'Candidate state is outside offered region')
        return r.fill(choice, [state.ref]) if choice else r.options_mismatch(state.value, [state.ref])
    if city:
        if re.search(r'^location|location \(?city\)?', q) and state and country and not field.options:
            return r.fill(f'{city.value}, {state.value}, {country.value}', [city.ref, state.ref, country.ref])
        choice = pick_text(field.options, city.value) if field.options else city.value
        return r.fill(choice, [city.ref]) if choice else r.options_mismatch(city.value, [city.ref])
    return r.unknown('Location not recorded', needs='personal.city')


def _compensation(r, field, q, db):
    current = bool(re.search(r'current|present|existing|last drawn|annual (?:base )?(?:salary|ctc) now', q))
    expected = bool(re.search(r'expect|desired|minimum|ask|looking for|target|requirement', q))
    if current and expected:
        c, e = _fact(db, 'compensation.current', 'currency:INR'), _fact(db, 'compensation.expected', 'currency:INR')
        if not (c and e) or field.options or field.kind == 'number':
            return r.unknown('Combined compensation question needs both saved values in free text', needs='compensation.current/expected')
        return r.fill(f'Current: {c.value}. Expected: {e.value}.', [c.ref, e.ref])
    if not current and not expected:
        return r.unknown('Unclear whether this asks for current or expected compensation', needs='form review')
    fact = _fact(db, 'compensation.current' if current else 'compensation.expected', 'currency:INR')
    if not fact:
        return r.unknown('Compensation is not recorded', needs='compensation')
    m = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*(?:lpa|lakhs?|lacs?|l)\s*(?:per annum|p\.?a\.?)?\s*', str(fact.value), re.I)
    if not m:
        return r.unknown(f'Saved compensation "{fact.value}" is not in LPA', needs='compensation')
    lakhs = float(m.group(1))
    if re.search(r'\b(?:usd|dollars?|eur|euros?|gbp|pounds?|cad|aud|sgd)\b|[$€£]', q):
        return r.unknown('Compensation requested in a currency other than INR', needs='compensation in that currency')
    monthly = bool(re.search(r'per month|monthly|\bpm\b|/ ?month', q))
    in_lakhs = bool(re.search(r'\b(?:lpa|lakhs?|lacs?|in l)\b', q))
    explicit_inr = bool(re.search(r'\b(?:inr|rupees?)\b|₹', q))
    if not (in_lakhs or explicit_inr) and ctx_country(r) not in ('IN', None) and (field.options or field.kind == 'number'):
        return r.unknown('Compensation without a currency for a role outside India', needs='compensation in local currency')
    if field.options:
        rng_value = lakhs if in_lakhs or any(re.search(r'lpa|lakh|lac|\bl\b', o, re.I) for o in field.options) else lakhs * 100000
        choice = pick_number(field.options, rng_value, 1)
        return r.fill(choice, [fact.ref]) if choice else r.options_mismatch(fact.value, [fact.ref])
    if monthly:
        return r.fill(str(int(lakhs * 100000 / 12)), [fact.ref], 'Annual LPA converted to monthly INR')
    if in_lakhs:
        return r.fill(f'{lakhs:g}', [fact.ref])
    if explicit_inr or (ctx_country(r) == 'IN' and (field.kind == 'number' or re.search(r'\b(?:annual|per annum|numeric|number)\b', q))):
        return r.fill(str(int(lakhs * 100000)), [fact.ref], 'LPA converted to annual INR')
    if field.kind == 'number':
        return r.unknown('Numeric compensation without a currency', needs='compensation currency')
    return r.fill(str(fact.value), [fact.ref])


def _experience(r, field, q, q_raw, db, today):
    skills = skills_in(q_raw, db)
    generic = re.search(r'\b(?:total|overall|professional|work|industry|engineering|software(?: engineering| development)?|it|full.?time|post.?graduat|paid)\b', q)
    if not skills and not re.search(r'\b(?:experience|years?)\s+(?:in|with|using|working with|of using)\s+\S', q):
        generic = True
    if re.search(r'\brelevant\b', q) and not skills:
        return r.unknown('"Relevant" experience depends on the role; no saved answer', needs='candidate answer')
    if skills and not re.search(r'total experience|overall experience', q):
        result = kb.skill_experience(db, skills, today)
        if result is None:
            return r.unknown(f'No confirmed dated usage for {", ".join(skills)}',
                             needs='python jobpilot.py facts confirm-skill "<skill>" --start YYYY-MM [--end YYYY-MM]')
        years, evidence = result
    elif generic:
        include_interns = bool(re.search(r'intern', q))
        years, evidence = kb.total_experience(db, today, include_interns)
        if not evidence:
            return r.unknown('No dated employment records', needs='employment')
    else:
        return r.unknown('Could not identify the experience being asked about', needs='form review')
    if r.intent == 'experience.threshold':
        m = re.search(r'(less|more|fewer) than (\d+) years?', q)
        limit = float(m.group(2))
        return _bool_answer(r, years < limit if m.group(1) in ('less', 'fewer') else years > limit, evidence,
                            f'{years:.2f} years from dated records')
    if field.options:
        choice = pick_number(field.options, years, 1)
        if choice is None and all(pick_bool([o], True) or pick_bool([o], False) for o in field.options):
            m = re.search(r'(\d+)\s*\+?\s*years?', q)
            if m:
                return _bool_answer(r, years >= float(m.group(1)), evidence, f'{years:.2f} years from dated records')
        return r.fill(choice, evidence, f'{years:.2f} years from dated records') if choice else r.options_mismatch(f'{years:.1f}', evidence)
    m = re.search(r'(?:at least|minimum(?: of)?|\b)(\d+)\s*\+?\s*years?', q)
    if m and re.search(r'^(?:do|have|are) you', q):
        return _bool_answer(r, years >= float(m.group(1)), evidence, f'{years:.2f} years from dated records')
    decimals = 0 if re.search(r'whole number|integer|in whole years', q) else 1
    return r.fill(pick_number([], years, decimals), evidence, f'{years:.2f} years from dated records, rounded down')


SAVED_FREE_TEXT = (
    (r'primarily worked in a product', 'answer.product_company_experience'),
    (r'pricing.*billing.*payments|transaction.?based systems', 'answer.transaction_systems_experience'),
    (r'programming languages and frameworks|primary (?:programming )?languages', 'answer.primary_languages'),
    (r'career goals', 'answer.career_goals'),
)
STOP = set('a an the and or of to in on for with your you have has had do did does is are was were be been what which how why '
           'please describe tell us about share any example give experience work worked working project projects role used use '
           'using would like if that this it as at by from we our me my i can could will so such particular familiarity '
           'technology technologies'.split())


def _free_text(r, field, q, q_raw, db, ctx):
    for pattern, key in SAVED_FREE_TEXT:
        if re.search(pattern, q):
            fact = _fact(db, key)
            if fact:
                return r.fill(str(fact.value), [fact.ref], 'Candidate saved this answer')
    if re.search(r"why (?:do you want|are you interested|us\b|this (?:company|role)|join)|what (?:interests|excites|attracts|draws|motivates) you|why .{0,40}(?:company|team|mission)|cover letter", q):
        return r.unknown('Motivation for this employer needs the candidate\'s own words', needs='candidate answer')
    if re.search(r'high school|salary history|references?\b|reason for leaving|weakness', q):
        return r.unknown('No recorded evidence for this question', needs='candidate answer')
    skills = set(skills_in(q_raw, db))
    words = {w for w in re.findall(r'[a-z0-9+#.]{3,}', q) if w not in STOP}
    scored = []
    for card in kb.cards(db):
        card_skills = {kb.canonical_skill(s) for s in card['skills']}
        skill_hits = skills & card_skills
        tag_hits = [t for t in card['tags'] if re.search(r'(?<![a-z0-9])' + re.escape(norm(t)) + r'(?![a-z0-9])', q)]
        word_hits = words & set(re.findall(r'[a-z0-9+#.]{3,}', norm(card['claim'])))
        score = 4 * len(skill_hits) + 3 * len(tag_hits) + 0.5 * len(word_hits)
        if skill_hits or tag_hits:
            scored.append((score, card))
    covered = set().union(*({kb.canonical_skill(x) for x in c['skills']} for c in kb.cards(db))) if kb.cards(db) else set()
    uncovered = sorted(skills - covered)
    if uncovered:
        return r.unknown(f'No evidence card covers {", ".join(uncovered)}', needs='evidence card: ' + ', '.join(uncovered))
    scored.sort(key=lambda x: -x[0])
    chosen = [c for s, c in scored[:2] if s >= 3]
    if not chosen:
        return r.unknown('No evidence card matches this question closely enough', needs='candidate answer or evidence card')
    text = ' '.join(c['claim'].rstrip('.') + '.' for c in chosen)
    return r.fill(text, [f"card:{c['id']}" for c in chosen], 'Composed from cited evidence cards')


# ------------------------------------------------------------------ per-application context

import contextvars

_CONTEXT: contextvars.ContextVar[Context | None] = contextvars.ContextVar('jobpilot_answer_context', default=None)
_NOTES: contextvars.ContextVar[list | None] = contextvars.ContextVar('jobpilot_answer_notes', default=None)


def context_for(job) -> Context:
    keys = job.keys() if hasattr(job, 'keys') else job
    get = lambda k: (job[k] if k in keys else None) or ''
    return Context(job_id=job['id'] if 'id' in keys else None, employer=get('company'), job_country=job_country(get('location')),
                   location=get('location'), source=get('source'), ats=get('ats'))


def set_context(job) -> Context:
    ctx = context_for(job)
    _CONTEXT.set(ctx)
    _NOTES.set([])
    return ctx


def current_context() -> Context:
    return _CONTEXT.get() or Context()


def note(result: Resolution, question: str) -> None:
    notes = _NOTES.get()
    if notes is not None:
        notes.append((question[:300], result))


def blockers() -> list[tuple[str, Resolution]]:
    return [(q, r) for q, r in (_NOTES.get() or []) if r.action == BLOCK]
