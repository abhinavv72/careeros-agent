import os
import re
import sys
import time
import json
import urllib.request
import subprocess
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CHALLENGE_FLAG = DATA / "captcha_challenge.json"
RESUME_FLAG = DATA / "captcha_resume.flag"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def play_alert_sound() -> None:
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except Exception:
        pass


def send_desktop_notification(title: str, message: str) -> None:
    play_alert_sound()
    try:
        toast_script = ROOT / "scripts" / "send_toast.ps1"
        if toast_script.exists():
            subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(toast_script), "-Title", title, "-Message", message],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        else:
            ps_cmd = f"""[void] [System.Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms'); $notify = New-Object System.Windows.Forms.NotifyIcon; $notify.Icon = [System.Drawing.SystemIcons]::Information; $notify.BalloonTipTitle = '{title}'; $notify.BalloonTipText = '{message}'; $notify.Visible = $True; $notify.ShowBalloonTip(4000); Start-Sleep -Seconds 4; $notify.Dispose()"""
            subprocess.Popen(["powershell", "-NoProfile", "-Command", ps_cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def is_captcha_present(page) -> bool:
    """Check if any CAPTCHA, Cloudflare Turnstile, or security challenge is visible."""
    try:
        # Check iframes
        recaptcha_frames = page.locator("iframe[src*='recaptcha']:visible, iframe[title*='reCAPTCHA']:visible")
        if recaptcha_frames.count():
            return True

        turnstile_frames = page.locator("iframe[src*='challenges.cloudflare.com']:visible, [class*='cf-turnstile']:visible")
        if turnstile_frames.count():
            return True

        hcaptcha_frames = page.locator("iframe[src*='hcaptcha.com']:visible")
        if hcaptcha_frames.count():
            return True

        # Check body text for security challenges
        body_text = page.locator("body").inner_text(timeout=2000).lower()
        challenge_markers = (
            "verify you are human",
            "confirm you are human",
            "security verification",
            "complete the security check",
            "unusual traffic from your computer",
            "cloudflare ray id",
            "attention required! | cloudflare",
        )
        if any(marker in body_text for marker in challenge_markers):
            return True

        return False
    except Exception:
        return False


def solve_recaptcha_audio(page) -> bool:
    """Attempt free audio-based reCAPTCHA v2 solving."""
    try:
        import speech_recognition as sr
    except ImportError:
        return False

    try:
        # 1. Locate anchor checkbox iframe
        anchor_frame = None
        for frame in page.frames:
            if "recaptcha" in frame.url and "anchor" in frame.url:
                anchor_frame = frame
                break

        if anchor_frame:
            checkbox = anchor_frame.locator("#recaptcha-anchor")
            if checkbox.count() and checkbox.is_visible():
                is_checked = checkbox.get_attribute("aria-checked") == "true"
                if not is_checked:
                    checkbox.click()
                    page.wait_for_timeout(2000)
                    if checkbox.get_attribute("aria-checked") == "true":
                        print("reCAPTCHA v2 passed via initial click.", flush=True)
                        return True

        # 2. Check if challenge popup (bframe) opened
        bframe = None
        for frame in page.frames:
            if "recaptcha" in frame.url and "bframe" in frame.url:
                bframe = frame
                break

        if not bframe:
            return False

        # 3. Locate and click audio challenge button
        audio_btn = bframe.locator("#recaptcha-audio-button")
        if not audio_btn.count() or not audio_btn.is_visible():
            return False

        audio_btn.click()
        page.wait_for_timeout(2500)

        # 4. Check if Google blocked audio challenge
        frame_text = bframe.locator("body").inner_text(timeout=2000).lower()
        if "automated queries" in frame_text or "try again later" in frame_text:
            print("Google rate-limited reCAPTCHA audio challenge. Falling back to human solve.", flush=True)
            return False

        # 5. Extract audio source URL
        audio_source = bframe.locator("#audio-source, .rc-audiochallenge-tdownload-link").first
        if not audio_source.count():
            return False

        audio_url = audio_source.get_attribute("src") or audio_source.get_attribute("href")
        if not audio_url:
            return False

        # 6. Download audio payload
        temp_dir = DATA / "temp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        audio_mp3 = temp_dir / f"recaptcha_{int(time.time())}.mp3"
        audio_wav = temp_dir / f"recaptcha_{int(time.time())}.wav"

        urllib.request.urlretrieve(audio_url, audio_mp3)

        # Convert MP3 to WAV using pydub or wave
        try:
            from pydub import AudioSegment
            sound = AudioSegment.from_mp3(audio_mp3)
            sound.export(audio_wav, format="wav")
            audio_file_for_recognition = audio_wav
        except Exception:
            audio_file_for_recognition = audio_mp3

        # 7. Recognize speech via free Google Web Speech API
        recognizer = sr.Recognizer()
        with sr.AudioFile(str(audio_file_for_recognition)) as source:
            audio_data = recognizer.record(source)

        transcript = recognizer.recognize_google(audio_data)
        if not transcript:
            return False

        print(f"reCAPTCHA audio transcribed: '{transcript}'", flush=True)

        # 8. Type answer and submit
        response_input = bframe.locator("#audio-response")
        if response_input.count():
            response_input.fill(transcript)
            page.wait_for_timeout(300)
            verify_btn = bframe.locator("#recaptcha-verify-button")
            if verify_btn.count():
                verify_btn.click()
                page.wait_for_timeout(2500)

        # Clean up temp audio files
        try:
            if audio_mp3.exists(): audio_mp3.unlink()
            if audio_wav.exists(): audio_wav.unlink()
        except Exception:
            pass

        # Check if challenge is cleared
        return not is_captcha_present(page)

    except Exception as exc:
        print(f"Audio CAPTCHA solving attempt failed ({exc}). Falling back to human.", flush=True)
        return False


def solve_turnstile_interactive(page) -> bool:
    """Attempt non-intrusive interactive pass on Cloudflare Turnstile."""
    try:
        for frame in page.frames:
            if "challenges.cloudflare.com" in frame.url:
                checkbox = frame.locator("input[type='checkbox'], [role='checkbox'], #cf-stage, .ctp-checkbox-label")
                if checkbox.count() and checkbox.is_visible():
                    checkbox.click()
                    page.wait_for_timeout(3000)
                    if not is_captcha_present(page):
                        print("Turnstile passed via interactive checkbox.", flush=True)
                        return True
        return False
    except Exception:
        return False


def notify_and_wait_human(page, company: str = "", job_id: int | str = "", timeout: int = 600) -> bool:
    """Notify user via sound and desktop notification, pause, and wait for human to solve."""
    DATA.mkdir(parents=True, exist_ok=True)
    challenge_data = {
        "job_id": job_id,
        "company": company or "Unknown Employer",
        "url": page.url,
        "status": "WaitingForUser",
        "detected_at": now(),
    }
    CHALLENGE_FLAG.write_text(json.dumps(challenge_data, indent=2), encoding="utf-8")

    title = "JobPilot: CAPTCHA Detected"
    msg = f"Solve the security challenge for {company or 'employer'} in Chrome, then click Continue."
    print(f"\n==================================================================", flush=True)
    print(f"[CAPTCHA PAUSE] Security challenge detected on {company or page.url}.", flush=True)
    print(f"1. Switch to the open Chrome window.", flush=True)
    print(f"2. Solve the challenge / check the box.", flush=True)
    print(f"3. Click 'Continue' in the JobPilot Dashboard or press Enter here.", flush=True)
    print(f"==================================================================\n", flush=True)

    send_desktop_notification(title, msg)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # Check if user clicked resume in dashboard
        if RESUME_FLAG.exists():
            RESUME_FLAG.unlink()
            if CHALLENGE_FLAG.exists(): CHALLENGE_FLAG.unlink()
            print("Resume signal received from dashboard.", flush=True)
            return True

        # Check if CAPTCHA was naturally solved in the browser
        if not is_captcha_present(page):
            if CHALLENGE_FLAG.exists(): CHALLENGE_FLAG.unlink()
            print("Security challenge solved successfully in browser.", flush=True)
            return True

        page.wait_for_timeout(1000)

    if CHALLENGE_FLAG.exists():
        CHALLENGE_FLAG.unlink()
    print("CAPTCHA verification timed out after 10 minutes.", flush=True)
    return False


def solve_captcha(page, company: str = "", job_id: int | str = "", timeout: int = 600) -> bool:
    """Master entry point to solve CAPTCHA using free automatic solver with human fallback."""
    if not is_captcha_present(page):
        return True

    print("CAPTCHA / Security challenge detected. Attempting automated free resolution...", flush=True)

    # 1. Try Turnstile click if Turnstile is present
    if any("challenges.cloudflare.com" in f.url for f in page.frames):
        if solve_turnstile_interactive(page):
            return True

    # 2. Try free audio solver if reCAPTCHA is present
    if any("recaptcha" in f.url for f in page.frames):
        if solve_recaptcha_audio(page):
            return True

    # 3. Fall back to Human-in-the-loop with dashboard alert & desktop notification
    return notify_and_wait_human(page, company=company, job_id=job_id, timeout=timeout)
