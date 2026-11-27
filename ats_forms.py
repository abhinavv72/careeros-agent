"""Local ATS adapters and form inspection; no AI service or paid dependencies."""
from dataclasses import dataclass
import re
from urllib.parse import urlparse


@dataclass(frozen=True)
class Adapter:
    resume: str
    submit: str


ADAPTERS = {
    'bamboohr': Adapter("input[type=file][name*='resume' i], input[type=file][id*='resume' i], input[type=file]", "button[type=submit]:visible, input[type=submit]:visible, button:visible"),
    "greenhouse": Adapter(
        "input[type=file]#resume, input[type=file][name*='resume' i]",
        "#submit_app:visible, #submit:visible, button[type=submit]:visible",
    ),
    "lever": Adapter(
        "input[type=file].resume-upload, input[type=file][name='resume']",
        "button[type=submit]:visible, [data-qa='btn-submit']:visible, button[data-qa='btn-submit']:visible",
    ),
    "ashby": Adapter(
        "input[type=file][name*='resume' i], input[type=file][id*='resume' i]",
        "button[type=submit]:visible",
    ),
    "smartrecruiters": Adapter(
        "input[type=file][name*='resume' i]",
        "button[type=submit]:visible, button[data-ui='button-submit']:visible",
    ),
    "workday": Adapter(
        "input[type=file], input[data-automation-id='file-upload-input']",
        "button[data-automation-id='submit-button']:visible, button[data-automation-id='bottom-navigation-next-button']:visible, button[type=submit]:visible",
    ),
}

# The presence of selectors or an Apply button is not enough to authorize an
# unattended submission. `tested` is enabled only for ATS paths with local
# end-to-end submit and fresh-confirmation coverage.
CAPABILITY_REGISTRY = {
    'greenhouse': {'discover': True, 'inspect': True, 'fill': True, 'submit': True, 'confirm': True, 'tested': True},
    'lever': {'discover': True, 'inspect': True, 'fill': True, 'submit': True, 'confirm': True, 'tested': True},
    'naukri': {'discover': True, 'inspect': True, 'fill': True, 'submit': True, 'confirm': True, 'tested': True},
    # LinkedIn prohibits third-party software that scrapes, modifies or
    # automates its site. Keep listings as manual handoffs only.
    'linkedin': {'discover': False, 'inspect': False, 'fill': False, 'submit': False, 'confirm': False, 'tested': False},
    'bamboohr': {'discover': False, 'inspect': True, 'fill': True, 'submit': False, 'confirm': False, 'tested': False},
    'ashby': {'discover': True, 'inspect': True, 'fill': True, 'submit': True, 'confirm': True, 'tested': True},
    'workday': {'discover': True, 'inspect': True, 'fill': True, 'submit': True, 'confirm': True, 'tested': True},
    'smartrecruiters': {'discover': True, 'inspect': True, 'fill': True, 'submit': False, 'confirm': False, 'tested': False},
    'indeed': {'discover': True, 'inspect': True, 'fill': True, 'submit': False, 'confirm': False, 'tested': False},
    'remotive': {'discover': True, 'inspect': False, 'fill': False, 'submit': False, 'confirm': False, 'tested': False},
    'himalayas': {'discover': True, 'inspect': False, 'fill': False, 'submit': False, 'confirm': False, 'tested': False},
    'google_jobs': {'discover': False, 'inspect': False, 'fill': False, 'submit': False, 'confirm': False, 'tested': False},
    'wellfound': {'discover': True, 'inspect': False, 'fill': False, 'submit': False, 'confirm': False, 'tested': False},
    'instahyre': {'discover': True, 'inspect': False, 'fill': False, 'submit': False, 'confirm': False, 'tested': False},
    'hirist': {'discover': True, 'inspect': False, 'fill': False, 'submit': False, 'confirm': False, 'tested': False},
    'trueup': {'discover': True, 'inspect': False, 'fill': False, 'submit': False, 'confirm': False, 'tested': False},
    'offcampus_jobs4u': {'discover': True, 'inspect': False, 'fill': False, 'submit': False, 'confirm': False, 'tested': False},
    'icims': {'discover': True, 'inspect': True, 'fill': True, 'submit': False, 'confirm': False, 'tested': False},
    'taleo': {'discover': True, 'inspect': True, 'fill': True, 'submit': False, 'confirm': False, 'tested': False},
    'oracle': {'discover': True, 'inspect': True, 'fill': True, 'submit': False, 'confirm': False, 'tested': False},
    'teamtailor': {'discover': True, 'inspect': True, 'fill': True, 'submit': False, 'confirm': False, 'tested': False},
    'jobvite': {'discover': True, 'inspect': True, 'fill': True, 'submit': False, 'confirm': False, 'tested': False},
    'workable': {'discover': True, 'inspect': True, 'fill': True, 'submit': False, 'confirm': False, 'tested': False},
    'generic': {'discover': False, 'inspect': True, 'fill': True, 'submit': False, 'confirm': False, 'tested': False},
}


def adapter_capabilities(ats: str) -> dict:
    values = CAPABILITY_REGISTRY.get(str(ats or '').lower())
    if values is None:
        return {'discover': False, 'inspect': False, 'fill': False, 'submit': False, 'confirm': False, 'tested': False}
    return dict(values)


def supports_autosubmit(ats: str) -> bool:
    capability = adapter_capabilities(ats)
    return bool(capability['submit'] and capability['confirm'] and capability['tested'])
GENERIC = Adapter(
    "input[type=file][name*='resume' i], input[type=file][id*='resume' i]",
    "button[type=submit]:visible, input[type=submit]:visible",
)


class FormSurface:
    """Query an application frame while retaining the parent page's controls."""

    def __init__(self, page, frame):
        self.page, self.frame = page, frame

    def locator(self, *args, **kwargs):
        return self.frame.locator(*args, **kwargs)

    def get_by_text(self, *args, **kwargs):
        return self.frame.get_by_text(*args, **kwargs)

    def get_by_role(self, *args, **kwargs):
        return self.frame.get_by_role(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.page, name)


def application_surface(page, ats: str):
    """Recognize embedded ATS forms; never select an unrelated iframe."""
    if ats == "greenhouse":
        for frame in page.frames:
            host = urlparse(frame.url).hostname or ""
            if host == "greenhouse.io" or host.endswith(".greenhouse.io"):
                if frame.locator("input[type=file], #first_name, input[type=email]").count():
                    return FormSurface(page, frame)
    return page


APPLICATION_FIELDS = (
    'input[type=email]:visible, input[type=tel]:visible, '
    'input[name=email i]:visible, input[id=email i]:visible, '
    'input[autocomplete=given-name]:visible, input[autocomplete=family-name]:visible, '
    'input[name=firstname i]:visible, input[name=first_name i]:visible, '
    'input[id=first_name]:visible, input[name=lastname i]:visible, '
    'form:visible input[type=file]:visible, [role=dialog]:visible input[type=file]:visible'
)
ENTRY_LABEL = re.compile(r'^\s*(?:apply for (?:this|the) (?:job|position|role)|apply(?: now)?|start (?:your )?application|apply manually)\s*$',re.I)
FINAL_LABEL = re.compile(r'^\s*(?:submit(?: (?:your )?(?:job )?application)?|apply(?: now)?|send (?:your )?application|complete (?:your )?application)\s*$',re.I)


def application_entry(surface):
    # Some custom ATS buttons expose a different accessible name from their text.
    text_controls=surface.locator('button:visible, a:visible, [role=button]:visible').filter(has_text=ENTRY_LABEL)
    named_controls=surface.get_by_role('button',name=ENTRY_LABEL).or_(surface.get_by_role('link',name=ENTRY_LABEL)).and_(surface.locator(':visible'))
    return text_controls.or_(named_controls)


def prepare_application(page, ats: str):
    """Wait for identity fields or an entry action; search inputs are not forms."""
    try:
        page.locator("button:has-text('Accept All'), button:has-text('Accept all cookies'), button:has-text('I Agree'), #onetrust-accept-btn-handler, [aria-label='Close']").first.click(timeout=1500)
    except Exception:
        pass
    surface=application_surface(page,ats)
    fields=surface.locator(APPLICATION_FIELDS)
    entry=application_entry(surface)
    signals=fields.or_(entry)
    if surface is page and ats=='greenhouse':
        signals=signals.or_(page.locator("iframe[src*='greenhouse.io']:visible"))
    try:
        signals.first.wait_for(state='visible',timeout=15000)
    except Exception:
        pass
    if surface is page and ats=='greenhouse' and page.locator("iframe[src*='greenhouse.io']:visible").count():
        try:
            page.frame_locator("iframe[src*='greenhouse.io']:visible").first.locator(APPLICATION_FIELDS).first.wait_for(state='visible',timeout=15000)
        except Exception:
            pass
        surface=application_surface(page,ats)
        fields=surface.locator(APPLICATION_FIELDS)
        entry=application_entry(surface)
    # A lone visible field (job-alert/newsletter email) on a listing page is not the
    # application form. When fewer than two form fields show and an explicit entry
    # action exists outside any form, open the application first.
    stray_field = 0 < fields.count() < 2
    hidden_embed = surface is not page and not fields.count()
    outside = []
    if stray_field or hidden_embed:
        if hidden_embed and not entry.count():
            # Embedded form hidden behind a page-level "Apply"/"Application" tab.
            entry=application_entry(page)
        # Only entry actions outside a form: an in-form "Apply" can be a final submit.
        outside=[c for c in entry.all() if not c.evaluate("el => !!el.closest('form,[role=dialog]')")]
    if outside:
        outside[0].click()
        surface=wait_for_application_surface(page,ats)
    elif not fields.count() and entry.count():
        controls=entry.all()
        if controls:
            # Duplicate header/sidebar entry buttons are common; each is an entry action,
            # never a final submission. Wait for the actual form after one click.
            controls[0].click()
            surface=application_surface(page,ats)
            if ats == "workday" or "myworkdayjobs.com" in getattr(page, "url", ""):
                try:
                    manual_btn = page.locator("a:visible, button:visible, [role=button]:visible, [role=link]:visible").filter(
                        has_text=re.compile(r"apply manually", re.I)
                    ).first
                    if manual_btn.is_visible(timeout=5000):
                        manual_btn.click()
                except Exception:
                    pass
            try:
                surface.locator(APPLICATION_FIELDS).first.wait_for(state='visible',timeout=15000)
            except Exception:
                pass
    try:
        page.locator("button:has-text('Accept All'), button:has-text('Accept all cookies'), button:has-text('I Agree'), #onetrust-accept-btn-handler, [aria-label='Close']").first.click(timeout=1500)
    except Exception:
        pass
    return surface




def wait_for_application_surface(page, ats: str, timeout=15000):
    """After an entry click, re-resolve the surface until visible form fields appear.

    Embedded ATS iframes (e.g. Greenhouse job_app) often load after the click, so a
    single application_surface() call can return the page before the frame is ready.
    """
    surface = application_surface(page, ats)
    waited = 0
    while waited < timeout:
        count = surface.locator(APPLICATION_FIELDS).count()
        # A single stray page field (footer newsletter box) must not end the wait
        # before a late-loading embedded form frame appears.
        if count >= 2 or (surface is not page and count):
            return surface
        page.wait_for_timeout(500)
        waited += 500
        surface = application_surface(page, ats)
    return surface


def posting_closed(page, apply_url: str) -> bool:
    """Greenhouse redirects removed postings to the board index (often with ?error=true)."""
    current = urlparse(getattr(page, "url", "") or "")
    host = current.hostname or ""
    if not (host == "greenhouse.io" or host.endswith(".greenhouse.io")):
        return False
    if "error=true" in (current.query or ""):
        return True
    had_job = re.search(r"/jobs/\d+", urlparse(apply_url or "").path or "")
    return bool(had_job) and not re.search(r"/jobs/\d+", current.path or "")


def submit_diagnostics(surface):
    return surface.locator('button:visible, input[type=submit]:visible, [role=button]:visible').evaluate_all("""els => els.map(el => ({
        text:(el.innerText || el.value || '').trim(),label:el.getAttribute('aria-label') || '',
        type:el.getAttribute('type'),disabled:el.disabled || el.getAttribute('aria-disabled')==='true',
        form:!!el.closest('form,[role=dialog]')
    }))""")


def residence_requirement(text, profile):
    """An explicit current-residence condition is different from relocation willingness."""
    match=re.search(r'(?:this )?role requires (?:being|you to be) (?:physically )?located in ([a-zA-Z][a-zA-Z ()-]{1,60})[.!?]',text,re.I)
    if not match or not re.search(r'confirm.{0,80}currently (?:located|living|resid)',text,re.I):return None
    required=match.group(1).strip()
    personal=profile.get('personal',{})
    current=[str(personal.get(key,'')).strip() for key in ('country','state','city')]
    if required.casefold() in [value.casefold() for value in current if value]:return None
    return f'Employer requires current residence in {required}; verify eligibility before applying'


# ---------------------------------------------------------------------------
# Typeahead / autocomplete helper
# ---------------------------------------------------------------------------

def fill_typeahead_field(page, field, value: str, matching_option_fn) -> bool:
    """Fill a location/autocomplete field by typing key-by-key so React
    autocomplete fires, then select the best matching dropdown option.

    Returns True if a dropdown option was selected, False if field was filled
    without dropdown confirmation (still a useful partial result).
    """
    try:
        field.click()
        field.evaluate(
            """(el, val) => {
                try {
                    const proto = el.tagName === 'TEXTAREA'
                        ? window.HTMLTextAreaElement.prototype
                        : window.HTMLInputElement.prototype;
                    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                    if (setter) setter.call(el, ''); else el.value = '';
                } catch(e) { el.value = ''; }
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            value,
        )
        # Type naturally so autocomplete JS fires on each keystroke
        page.keyboard.type(value, delay=55)
        dropdown = page.locator("[role=option]:visible, [role=listbox] li:visible, .dropdown-results > *:visible, .location-option:visible, [class*='typeahead'] li:visible, [class*='suggestion']:visible, [class*='autocomplete'] li:visible, .pac-item:visible")
        try:dropdown.first.wait_for(state='visible',timeout=2000)
        except Exception:pass  # Plain text city inputs do not have a dropdown.

        # Try dropdown options first
        option_selectors = [
            "[role='option']:visible",
            "[role='listbox'] li:visible",
            ".dropdown-results > *:visible",
            ".location-option:visible",
            "[class*='typeahead'] li:visible",
            "[class*='suggestion']:visible",
            "[class*='autocomplete'] li:visible",
            ".pac-item:visible",  # Google Places
        ]
        for selector in option_selectors:
            options = page.locator(selector)
            if options.count():
                candidates = options.all()
                texts = [o.inner_text() for o in candidates]
                idx = matching_option_fn(value, texts)
                if idx is not None:
                    candidates[idx].click()
                    return True
                # Close dropdown if we can't match
                page.keyboard.press("Escape")
                break
        return False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Scroll all lazy-rendered card sections into view (Lever custom cards)
# ---------------------------------------------------------------------------

def scroll_all_cards(surface) -> None:
    """Scroll through the entire Lever application form so all lazy card
    sections become visible and their inputs are added to the DOM."""
    try:
        expanded = surface.evaluate(
            """() => {
                const form = document.querySelector('form, .application-form, [data-qa="application-form"]') || document.body;
                // Scroll through the form in chunks to trigger intersection observers
                const totalHeight = form.scrollHeight;
                const step = 400;
                for (let y = 0; y <= totalHeight; y += step) {
                    form.scrollTo && form.scrollTo(0, y);
                    window.scrollTo && window.scrollTo(0, y);
                }
                // Expand any collapsed accordion/card sections
                let changed = false;
                document.querySelectorAll('[data-qa^="card-"], .application-card, .custom-question-card').forEach(card => {
                    if (card.getAttribute('aria-expanded') === 'false') {
                        card.click && card.click(); changed = true;
                    }
                });
                return changed;
            }"""
        )
        if expanded:
            surface.wait_for_timeout(250)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Field inspection
# ---------------------------------------------------------------------------

INSPECT_JS = r"""els => {
    const shown = el => el.getAttribute('aria-hidden') !== 'true'
        && !!(el.getClientRects().length)
        && getComputedStyle(el).visibility !== 'hidden';

    // Resolve a visible label for any form element, including Lever custom cards
    const label = el => {
        // Standard HTML label association
        const htmlLabels = [...(el.labels || [])].map(l => l.innerText).join(' ').trim();
        if (htmlLabels) return htmlLabels;

        // aria-label attribute
        const ariaLabel = el.getAttribute('aria-label');
        if (ariaLabel) return ariaLabel;

        // aria-labelledby
        const labelledBy = (el.getAttribute('aria-labelledby') || '').split(/\s+/)
            .map(id => document.getElementById(id)?.innerText || '').join(' ').trim();
        if (labelledBy) return labelledBy;

        // Lever custom card: name is a UUID-keyed input like cards[uuid][field0]
        // Walk up DOM to find the nearest heading or application-label
        const cardAncestors = [
            '.application-question', '.custom-question', '.posting-field',
            '[data-qa^="card-"]', '.application-card', '.custom-question-card',
            '.field', '[class*="question"]', '[class*="field"]',
        ];
        for (const sel of cardAncestors) {
            const box = el.closest(sel);
            if (box) {
                // Prefer explicit label elements inside the card
                const lbl = box.querySelector('.application-label, label, h4, h3, legend, .text, [class*="label"]');
                if (lbl && lbl.innerText.trim()) return lbl.innerText.trim();
                // Fallback: use the card's own text minus nested input values
                const cloned = box.cloneNode(true);
                cloned.querySelectorAll('input, textarea, select').forEach(n => n.remove());
                const text = cloned.innerText.trim();
                if (text) return text;
            }
        }

        // Parent element text as last resort
        const placeholder = el.placeholder;
        if (placeholder) return placeholder;
        const name = el.name || el.id;
        if (name && !name.startsWith('cards[')) return name;
        return el.parentElement ? el.parentElement.innerText.trim() || 'Unknown question' : 'Unknown question';
    };

    const radios = new Set(els.filter(el => el.type === 'radio' && el.checked).map(el => el.name));
    const checkedCheckboxes = new Set(els.filter(el => el.type === 'checkbox' && el.checked).map(el => el.name).filter(Boolean));
    const seen = new Set();
    return els.filter(el => shown(el) && !el.disabled).map(el => {
        const required = el.required || el.getAttribute('aria-required') === 'true';
        const selected = el.getAttribute('role') === 'combobox'
            ? (el.closest('.select__control, .select-shell, .select__container, .field-wrapper, .field, [class*="select-container"]')
                ?.querySelector('[class*="single-value"], [class*="singleValue"], [class*="multi-value__label"], [class*="multiValue__label"]')?.innerText
               || el.closest('.select__control, .select-shell, .select__container, .field-wrapper, .field, [class*="select-container"]')
                ?.querySelector('[class*="value-container--has-value"]')?.innerText || '')
            : '';
        const empty = el.type === 'radio' ? !radios.has(el.name)
            : el.type === 'checkbox' ? (!el.checked && (!el.name || !checkedCheckboxes.has(el.name)))
            : el.type === 'file' ? !el.files.length
            : !String(selected || el.value || '').trim();
        const invalid = el.validity ? !el.validity.valid : el.getAttribute('aria-invalid') === 'true';
        const row = {
            question: label(el),
            name: el.name || el.id,
            type: el.type || el.tagName.toLowerCase(),
            required, missing: required && empty, invalid: !empty && invalid,
            options: el.tagName === 'SELECT'
                ? [...el.options].filter(o => !o.disabled && o.value).map(o => o.text)
                : [],
        };
        if (el.type === 'radio') { if (seen.has(el.name)) return null; seen.add(el.name); }
        if (el.type === 'checkbox' && el.name) { if (seen.has(el.name)) return null; seen.add(el.name); }
        return row;
    }).filter(Boolean);
}"""


def inspect_fields(surface):
    return surface.locator(
        "input:not([type=hidden]):not([type=submit]):not([type=button]),"
        " textarea, select, [role=combobox][aria-required=true]"
    ).evaluate_all(INSPECT_JS)


def unresolved_fields(surface):
    return [row for row in inspect_fields(surface) if row["missing"] or row["invalid"]]


def collect_question_options(surface, rows):
    """Inspect unanswered custom dropdowns without selecting an answer."""
    for control in surface.locator("[role=combobox]:visible").all():
        key = control.get_attribute("name") or control.get_attribute("id")
        row = next((row for row in rows if row["name"] == key), None)
        if row is None or row["options"]:
            continue
        try:
            control.click()
            row["options"] = [
                option.inner_text()
                for option in surface.locator("[role=option]:visible").all()
            ]
        finally:
            surface.keyboard.press("Escape")
    return rows


# ---------------------------------------------------------------------------
# Submission and navigation controls
# ---------------------------------------------------------------------------

# Confirmation phrases that mean the application was accepted
CONFIRMATION_RE = re.compile(
    r"application submitted|thank you for applying|received your application"
    r"|your application has been submitted|we.ve received your application"
    r"|successfully submitted|application complete|application received"
    r"|you.ve successfully applied|application was submitted"
    r"|thanks for applying|your submission has been received",
    re.I,
)


def submission_control(surface, ats: str):
    """Recognize a unique explicit final action, independent of button styling."""
    candidates=surface.locator('button:visible, input[type=submit]:visible, [role=button]:visible').all()
    final=[]
    for control in candidates:
        tag=control.evaluate('el => el.tagName')
        text=control.evaluate('el => el.value') if tag=='INPUT' else control.inner_text()
        labels=[re.sub(r'\s+',' ',str(value or '')).strip() for value in (text,control.get_attribute('aria-label'))]
        label=next((value for value in labels if FINAL_LABEL.fullmatch(value)),None)
        if label is None:continue
        if not control.is_enabled() or control.get_attribute('aria-disabled')=='true':continue
        # Plain Apply/Apply Now outside an actual form is a job-entry action.
        if re.fullmatch(r'apply(?: now)?',label,re.I) and not control.evaluate("el => !!el.closest('form,[role=dialog]')"):
            continue
        final.append(control)
    in_form = [c for c in final if c.evaluate("el => !!el.closest('form,[role=dialog]')")]
    if len(in_form) == 1:
        return in_form[0]
    if in_form:
        submit_specific = [c for c in in_form if re.search(r'submit', c.inner_text() or '', re.I)]
        if len(submit_specific) == 1:
            return submit_specific[0]
        if len(submit_specific) > 1 and len({c.inner_text().strip().lower() for c in submit_specific}) == 1:
            return submit_specific[-1]
        texts = {c.inner_text().strip().lower() for c in in_form}
        if len(texts) == 1:
            return in_form[-1]
    if len(final) == 1:
        return final[0]
    if ats in ADAPTERS and ADAPTERS[ats].submit:
        adapter_btns = surface.locator(ADAPTERS[ats].submit).all()
        active = [b for b in adapter_btns if b.is_visible() and b.is_enabled() and b.get_attribute('aria-disabled') != 'true']
        if active:
            return active[-1]
    return None


def wait_for_submission_control(surface, ats: str, timeout=10000):
    control=submission_control(surface,ats)
    if control is not None:return control
    candidate=surface.get_by_role('button',name=FINAL_LABEL).and_(surface.locator(':visible'))
    try:
        candidate.first.wait_for(state='visible',timeout=timeout)
        # Trial checks actionability without submitting anything.
        candidate.first.click(trial=True,timeout=timeout)
    except Exception:return None
    return submission_control(surface,ats)


def next_application_control(surface):
    """Find one application navigation control, scoped away from search pagination."""
    containers = surface.locator("form:visible, [role=dialog]:visible")
    if containers.count() != 1:
        return None
    candidates = containers.first.get_by_role(
        "button", name=re.compile(r"^\s*(next|continue|review application)\s*$", re.I)
    ).all()
    enabled = [button for button in candidates if button.is_visible() and button.is_enabled()]
    return enabled[0] if len(enabled) == 1 else None
