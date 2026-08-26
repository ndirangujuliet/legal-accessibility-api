"""
Haki Legal Aid — USSD + SMS civic-tech app
--------------------------------------------------
Built for a hackathon: makes legal rights info and case reporting
accessible over USSD/SMS so it works on any phone, not just smartphones.

Core flow (all driven by Africa's Talking USSD callback):
  0. Main menu:      Know Your Rights / Legal Aid Hotline / Report Case / Track My Case
  1. Rights menu:    pick a topic -> short summary shown on USSD + full text via SMS
  2. Hotline:        show the number on-screen + SMS it for later reference
  3. Report Case:    multi-step anonymous report -> generates a tracking code -> SMS'd to user
  4. Track My Case:  enter a tracking code -> see current status

NOTE ON ANONYMITY: Africa's Talking always passes the caller's phoneNumber to
the USSD callback (needed to serve the session and send the SMS), so a USSD
report is never fully anonymous to the telco. What THIS app does to protect
the reporter is: it never stores the raw phone number against the report —
only a one-way hash of it (see `_pseudonymise`). That way even if the report
database leaks, individual reporters can't be identified from it, but abuse
detection (e.g. rate-limiting spam reports) is still possible.
For real anonymity guarantees, pair this with a legal/policy review, not
just code.
"""

import os
import uuid
import hashlib
import logging
from datetime import datetime

from flask import Flask, request
from dotenv import load_dotenv
import africastalking

import sms_store

# Load variables from a local .env file into the environment. Must happen
# before we read any os.environ.get(...) calls below, so this import stays
# right after the other imports, not further down the file.
load_dotenv()

# ---------------------------------------------------------------------------
# 1. CONFIG & INITIALISATION
# ---------------------------------------------------------------------------

# Load credentials from environment variables rather than hardcoding them.
# Get sandbox credentials free at https://account.africastalking.com
# (Sandbox username is literally the string "sandbox".)
AT_USERNAME = os.environ.get("AT_USERNAME", "sandbox")
AT_API_KEY = os.environ.get("AT_API_KEY", "")  # set this in your .env / shell
AT_SENDER_ID = os.environ.get("AT_SENDER_ID", None)  # optional, sandbox ignores this

LEGAL_AID_HOTLINE = os.environ.get("LEGAL_AID_HOTLINE", "0800 720 000")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("haki-legal-aid")

app = Flask(__name__)

# Initialise the Africa's Talking SDK once at startup.
# If the API key is missing (e.g. first local run before .env is set up),
# we log a warning instead of crashing, so the USSD menus can still be
# demoed without SMS actually sending.
sms_client = None
try:
    if AT_API_KEY:
        africastalking.initialize(AT_USERNAME, AT_API_KEY)
        sms_client = africastalking.SMS
        logger.info("Africa's Talking SDK initialised for user '%s'", AT_USERNAME)
    else:
        logger.warning("AT_API_KEY not set — SMS sending is disabled until you configure it.")
except Exception as exc:  # noqa: BLE001 - top-level init guard, we want to catch anything here
    logger.error("Failed to initialise Africa's Talking SDK: %s", exc)

# Create the sms_logs table if it doesn't exist yet. Safe to call on every
# startup — it's a no-op if the table's already there.
sms_store.init_db()


# ---------------------------------------------------------------------------
# 2. "DATABASE" (in-memory for the hackathon demo)
# ---------------------------------------------------------------------------
# For a real deployment, swap this dict for SQLite/Postgres (see README).
# Keyed by tracking_code -> report record.
CASE_REPORTS = {}

# Static content for the "Know Your Rights" menu. In production this would
# likely come from a CMS or database so non-technical staff can update it.
RIGHTS_CONTENT = {
    "1": {
        "title": "Rights if Arrested",
        "summary": "You have the right to remain silent, to know why you're "
                    "being arrested, to a lawyer, and to be brought to court "
                    "within 24 hours.",
    },
    "2": {
        "title": "Employment Rights",
        "summary": "You're entitled to a written contract, minimum wage, "
                    "safe working conditions, and notice before dismissal.",
    },
    "3": {
        "title": "Tenant Rights",
        "summary": "A landlord must give written notice before eviction and "
                    "cannot lock you out or seize property without a court order.",
    },
}

SW_RIGHTS_CONTENT = {
    "1": {"title": "Haki ukikamatwa", "summary": "Una haki ya kukaa kimya, kujua sababu ya kukamatwa, kupata wakili, na kufikishwa kortini ndani ya saa 24."},
    "2": {"title": "Haki za ajira", "summary": "Una haki ya mkataba wa maandishi, mshahara wa chini, mazingira salama ya kazi, na taarifa kabla ya kufutwa kazi."},
    "3": {"title": "Haki za mpangaji", "summary": "Mwenye nyumba lazima akupe taarifa ya maandishi kabla ya kukufukuza na hawezi kukufungia nje bila amri ya korti."},
}

SW_CATEGORIES = {
    "1": "Mgogoro wa ardhi / mali",
    "2": "Tatizo la kazini",
    "3": "Tatizo la polisi / kukamatwa",
    "4": "Nyingine",
}

PETITIONS = {
    "1": {
        "title": "Protect Tenants from Illegal Eviction",
        "summary": "Support stronger protection and proper notice for tenants.",
        "action": "Community legal awareness meeting",
    },
    "2": {
        "title": "Improve Access to Legal Aid",
        "summary": "Support accessible legal services for every community.",
        "action": "Legal aid awareness walk",
    },
}

SW_PETITIONS = {
    "1": {
        "title": "Linda wapangaji dhidi ya kufukuzwa kinyume cha sheria",
        "summary": "Unga mkono ulinzi bora na taarifa sahihi kwa wapangaji.",
        "action": "Mkutano wa uhamasishaji wa sheria kwa jamii",
    },
    "2": {
        "title": "Boresha upatikanaji wa msaada wa kisheria",
        "summary": "Unga mkono huduma za kisheria zinazopatikana kwa kila jamii.",
        "action": "Matembezi ya uhamasishaji wa msaada wa kisheria",
    },
}

PETITION_SIGNERS = {}
ACTION_PARTICIPANTS = {}


# ---------------------------------------------------------------------------
# 3. HELPERS
# ---------------------------------------------------------------------------

def _pseudonymise(phone_number: str) -> str:
    """
    One-way hash of the phone number so we can detect abuse/duplicates
    without storing the number itself against a case report.
    Add a server-side secret salt (env var) in production so the hash
    can't be reversed via a rainbow table of Kenyan phone number formats.
    """
    salt = os.environ.get("PHONE_HASH_SALT", "change-me-in-production")
    return hashlib.sha256((salt + phone_number).encode("utf-8")).hexdigest()[:16]


def _new_tracking_code() -> str:
    """Short, human-readable tracking code, e.g. HK-3F9A2B."""
    return "HK-" + uuid.uuid4().hex[:6].upper()


def send_sms(phone_number: str, message: str) -> bool:
    """
    Sends an SMS via Africa's Talking. Returns True/False so callers can
    decide whether to tell the user "check your SMS" or not.
    Never raises — a failed SMS should not crash the USSD session, since
    the user is still mid-call and needs *some* response either way.

    Every attempt (sent or failed) is logged to sms_logs, keyed by a
    pseudonymised phone hash rather than the raw number — same privacy
    approach as case reports, see _pseudonymise.
    """
    phone_hash = _pseudonymise(phone_number)

    if sms_client is None:
        logger.warning("SMS not sent (client not configured): %s", message)
        sms_store.log_sms("outgoing", phone_hash, message, status="not_configured")
        return False

    try:
        response = sms_client.send(message, [phone_number], sender_id=AT_SENDER_ID)
        recipients = response.get("SMSMessageData", {}).get("Recipients", [])
        recipient = recipients[0] if recipients else {}
        at_message_id = recipient.get("messageId")
        delivery_status = str(recipient.get("status", "")).lower()
        accepted_statuses = {"success", "sent", "queued", "submitted"}
        sms_accepted = delivery_status in accepted_statuses
        log_status = delivery_status or "unknown"

        logger.info(
            "SMS API response for %s: status=%s message_id=%s response=%s",
            phone_number,
            log_status,
            at_message_id,
            response,
        )
        sms_store.log_sms(
            "outgoing",
            phone_hash,
            message,
            status=log_status,
            at_message_id=at_message_id,
        )
        return sms_accepted
    except Exception as exc:  # noqa: BLE001 - external API call, catch broadly and log
        logger.error("Failed to send SMS to %s: %s", phone_number, exc)
        sms_store.log_sms("outgoing", phone_hash, message, status="failed")
        return False


# ---------------------------------------------------------------------------
# 4. USSD CALLBACK
# ---------------------------------------------------------------------------
# Africa's Talking POSTs here on every key-press in the session. The whole
# journey so far arrives as `text`, star-separated, e.g. after a user
# dials the shortcode then presses 3 then 1, `text` = "3*1".
#
# Response format required by Africa's Talking:
#   "CON <message>"  -> keep the session open, show <message>, wait for more input
#   "END <message>"  -> show <message> and terminate the session
# ---------------------------------------------------------------------------

@app.route("/ussd", methods=["GET", "POST"])
@app.route("/ussd/", methods=["GET", "POST"])
def ussd_callback():
    if request.method == "GET":
        return "USSD endpoint is live. Send POST requests from Africa's Talking.", 200, {
            "Content-Type": "text/plain"
        }

    try:
        session_id = request.values.get("sessionId", "")
        phone_number = request.values.get("phoneNumber", "")
        text = request.values.get("text", "")

        logger.info(
            "USSD callback hit: session_id=%s phone=%s text='%s'",
            session_id,
            phone_number,
            text,
        )

        # Split the accumulated journey into individual steps.
        # text == ""  -> user has just dialled in, show the main menu.
        steps = text.split("*") if text else []

        response = _route(steps, phone_number)
        return response, 200, {"Content-Type": "text/plain"}

    except Exception as exc:  # noqa: BLE001 - last-resort guard for the whole session
        # Whatever goes wrong, the user is mid-call on a USSD session and
        # MUST get a valid CON/END response or their phone shows a generic
        # network error with no useful info. Fail closed with END, not a 500.
        logger.exception("Unhandled error in USSD callback: %s", exc)
        return "END Sorry, something went wrong. Please try again shortly.", 200, {
            "Content-Type": "text/plain"
        }


def _route(steps, phone_number):
    """Pure routing logic, separated from the Flask handler so it's easy to unit test."""

    # The first digit selects the language; the remaining digits are menu input.
    if len(steps) == 0:
        return "CON Chagua lugha / Choose language\n1. English\n2. Kiswahili"

    language = "sw" if steps[0] == "2" else "en" if steps[0] == "1" else None
    if language is None:
        return "END Invalid language. Dial again and choose 1 or 2."
    steps = steps[1:]
    if len(steps) == 0:
        if language == "sw":
            return "CON Karibu Haki Legal Aid\n1. Jua Haki Zako\n2. Nambari ya Msaada wa Kisheria\n3. Ripoti Kesi\n4. Fuatilia Kesi\n5. Petitions na Uhamasishaji"
        return "CON Welcome to Haki Legal Aid\n1. Know Your Rights\n2. Legal Aid Hotline\n3. Report a Case\n4. Track My Case\n5. Petitions and Mobilization"

    top = steps[0]

    # --- Branch 1: Know Your Rights -----------------------------------------
    if top == "1":
        if len(steps) == 1:
            menu = "CON Choose a topic:\n"
            content = SW_RIGHTS_CONTENT if language == "sw" else RIGHTS_CONTENT
            menu = "CON Chagua mada:\n" if language == "sw" else menu
            for key, item in content.items():
                menu += f"{key}. {item['title']}\n"
            return menu.rstrip()

        choice = steps[1]
        topic = (SW_RIGHTS_CONTENT if language == "sw" else RIGHTS_CONTENT).get(choice)
        if not topic:
            return "END Invalid option. Please dial in again and try another number."

        # Send the fuller text via SMS since USSD screens are tiny (~160 chars).
        sms_sent = send_sms(
            phone_number,
            (f"Haki Legal Aid — {topic['title']}: {topic['summary']} "
             f"Kwa msaada zaidi piga: {LEGAL_AID_HOTLINE}" if language == "sw" else
             f"Haki Legal Aid — {topic['title']}: {topic['summary']} For more help call our hotline: {LEGAL_AID_HOTLINE}"),
        )
        note = "Tumekutumia pia kwa SMS." if language == "sw" and sms_sent else "We've also sent this to you via SMS." if sms_sent else ""
        return f"END {topic['title']}: {topic['summary']} {note}".rstrip()

    # --- Branch 2: Legal Aid Hotline ----------------------------------------
    if top == "2":
        sms_sent = send_sms(
            phone_number,
            (f"Nambari ya Msaada wa Kisheria ya Haki Legal Aid: {LEGAL_AID_HOTLINE}. "
             f"Hifadhi nambari hii." if language == "sw" else
             f"Haki Legal Aid Hotline: {LEGAL_AID_HOTLINE}. Save this number — it's free to call and staffed by legal aid volunteers."),
        )
        note = "Tumekutumia nambari hii kwa SMS." if language == "sw" and sms_sent else "We've also texted you the number." if sms_sent else ""
        return f"END Legal Aid Hotline: {LEGAL_AID_HOTLINE}. {note}".rstrip()

    # --- Branch 3: Report a Case (anonymous) --------------------------------
    if top == "3":
        if len(steps) == 1:
            if language == "sw":
                return "CON Unaripoti kesi ya aina gani?\n1. Mgogoro wa ardhi / mali\n2. Tatizo la kazini\n3. Tatizo la polisi / kukamatwa\n4. Nyingine"
            return "CON What type of case are you reporting?\n1. Land / Property Dispute\n2. Workplace Issue\n3. Police / Arrest Issue\n4. Other"

        if len(steps) == 2:
            category_map = SW_CATEGORIES if language == "sw" else {"1": "Land / Property Dispute", "2": "Workplace Issue", "3": "Police / Arrest Issue", "4": "Other"}
            if steps[1] not in category_map:
                return "END Invalid option. Please dial in again and try another number."
            return "CON Eleza kwa ufupi kilichotokea:" if language == "sw" else "CON Briefly describe what happened (a few words is fine):"

        if len(steps) == 3:
            category_map = SW_CATEGORIES if language == "sw" else {"1": "Land / Property Dispute", "2": "Workplace Issue", "3": "Police / Arrest Issue", "4": "Other"}
            category = category_map.get(steps[1], "Other")
            # steps[2] onward is the free-text description. If the user's
            # description itself contained a "*", rejoin it here so we don't
            # lose anything after the first star.
            description = "*".join(steps[2:]).strip()

            if not description:
                return "END No description received. Please dial in again to report your case."

            tracking_code = _new_tracking_code()
            CASE_REPORTS[tracking_code] = {
                "category": category,
                "description": description,
                "reporter_hash": _pseudonymise(phone_number),
                "status": "Received",
                "court_date": None,
                "created_at": datetime.utcnow().isoformat(),
            }

            sms_sent = send_sms(
                phone_number,
                (f"Haki Legal Aid: ripoti yako imepokelewa. Aina: {category}. "
                 f"Maelezo: {description}. Nambari ya ufuatiliaji: {tracking_code}." if language == "sw" else
                 f"Haki Legal Aid: your report was received. Type: {category}. Details: {description}. Tracking code: {tracking_code}. Dial back and choose 'Track My Case' with this code to check status. Keep it safe."),
            )
            note = "Imetumwa pia kwa SMS." if language == "sw" and sms_sent else "It's also been sent to you via SMS." if sms_sent else ""
            return (
                (f"END Ripoti imetumwa. Nambari yako ya ufuatiliaji ni {tracking_code}. {note}" if language == "sw" else f"END Report submitted. Your tracking code is {tracking_code}. {note}")
            ).rstrip()

    # --- Branch 4: Track My Case ---------------------------------------------
    if top == "4":
        if len(steps) == 1:
            return "CON Weka nambari yako ya ufuatiliaji (mf. HK-3F9A2B):" if language == "sw" else "CON Enter your tracking code (e.g. HK-3F9A2B):"

        code = steps[1].strip().upper()
        record = CASE_REPORTS.get(code)
        if not record:
            return "END Hakuna kesi iliyopatikana. Hakikisha nambari na ujaribu tena." if language == "sw" else "END No case found with that tracking code. Please check and try again."

        court_date = record.get("court_date") or ("Haijawekwa" if language == "sw" else "Not set")
        return (f"END Kesi {code} ({record['category']}): hali ni '{record['status']}'. Tarehe ya korti: {court_date}. Iliwasilishwa {record['created_at'][:10]}." if language == "sw" else f"END Case {code} ({record['category']}): status is '{record['status']}'. Court date reminder: {court_date}. Submitted {record['created_at'][:10]}.")

    # --- Branch 5: Petitions and mobilization -------------------------------
    if top == "5":
        petitions = SW_PETITIONS if language == "sw" else PETITIONS
        if len(steps) == 1:
            heading = "CON Chagua ombi la kusaini:" if language == "sw" else "CON Choose a petition to sign:"
            return heading + "\n" + "\n".join(
                f"{key}. {petition['title']}" for key, petition in petitions.items()
            )

        petition = petitions.get(steps[1])
        if not petition:
            return "END Chaguo si sahihi. Tafadhali jaribu tena." if language == "sw" else "END Invalid petition. Please try again."

        petition_id = steps[1]
        if len(steps) == 2:
            prompt = "1. Saini ombi\n2. Jiunge na hatua" if language == "sw" else "1. Sign petition\n2. Join action"
            return f"CON {petition['title']}\n{petition['summary']}\n{prompt}"

        if steps[2] == "1":
            PETITION_SIGNERS.setdefault(petition_id, set()).add(_pseudonymise(phone_number))
            sms_sent = send_sms(
                phone_number,
                (f"Haki Legal Aid: Umesaini ombi '{petition['title']}'. Asante kwa kushiriki."
                 if language == "sw" else
                 f"Haki Legal Aid: You signed the petition '{petition['title']}'. Thank you for participating."),
            )
            note = " Ujumbe umetumwa kwa SMS." if language == "sw" and sms_sent else " SMS confirmation sent." if sms_sent else ""
            return ("END Umesaini ombi. Asante kwa kushiriki." if language == "sw" else "END Petition signed. Thank you for participating.") + note

        if steps[2] == "2":
            ACTION_PARTICIPANTS.setdefault(petition_id, set()).add(_pseudonymise(phone_number))
            sms_sent = send_sms(
                phone_number,
                (f"Haki Legal Aid: Umejiunga na hatua '{petition['action']}' kuhusu '{petition['title']}'. Asante kwa kushiriki."
                 if language == "sw" else
                 f"Haki Legal Aid: You joined the action '{petition['action']}' for '{petition['title']}'. Thank you for participating."),
            )
            note = " Ujumbe umetumwa kwa SMS." if language == "sw" and sms_sent else " SMS confirmation sent." if sms_sent else ""
            return (f"END Umejiunga na hatua: {petition['action']}. Asante kwa kushiriki." if language == "sw" else f"END You joined the action: {petition['action']}. Thank you for participating.") + note

        return "END Chaguo si sahihi. Tafadhali jaribu tena." if language == "sw" else "END Invalid choice. Please try again."

    # --- Fallback -------------------------------------------------------------
    return "END Invalid option. Please dial in again and try another number."


# ---------------------------------------------------------------------------
# 5. EXTENSION POINT: updating a case status (e.g. from an admin/legal-aid
#    dashboard, NOT exposed over USSD). A real deployment would put this
#    behind authentication — shown here unauthenticated only for demo speed.
# ---------------------------------------------------------------------------

@app.route("/admin/cases/<tracking_code>/status", methods=["POST"])
def update_case_status(tracking_code):
    """
    Example of how a caseworker's dashboard could update a report's status,
    and how you'd notify the original reporter by SMS without ever knowing
    their identity from the report record itself (their phone number was
    never stored — see _pseudonymise). In practice you'd need a SEPARATE,
    access-controlled mapping of tracking_code -> phone_number if you want
    to notify people, stored more securely than the report content itself.
    """
    new_status = request.form.get("status")
    court_date = request.form.get("court_date")
    record = CASE_REPORTS.get(tracking_code.upper())

    if not record:
        return {"error": "tracking code not found"}, 404
    if not new_status:
        return {"error": "status field is required"}, 400

    record["status"] = new_status
    if court_date is not None:
        record["court_date"] = court_date.strip() or None
    logger.info("Case %s status updated to '%s'", tracking_code, new_status)
    return {
        "tracking_code": tracking_code,
        "status": new_status,
        "court_date": record.get("court_date"),
    }, 200


# ---------------------------------------------------------------------------
# 6. INCOMING SMS — Africa's Talking POSTs here whenever someone texts your
#    shortcode/number directly (not through a USSD session). Configure this
#    URL in the AT dashboard under SMS > Callback URLs > Incoming Messages.
#    AT sends form fields including: from, to, text, id, linkId, date.
# ---------------------------------------------------------------------------

@app.route("/sms/incoming", methods=["POST"])
def sms_incoming():
    try:
        sender = request.values.get("from", "")
        text = request.values.get("text", "")
        at_message_id = request.values.get("id", "")

        phone_hash = _pseudonymise(sender)
        sms_store.log_sms("incoming", phone_hash, text, at_message_id=at_message_id)

        logger.info("Incoming SMS logged (hash=%s): %s", phone_hash, text)

        # Africa's Talking just needs a 200 OK to know we received it —
        # no specific body format required for incoming SMS callbacks.
        return "", 200

    except Exception as exc:  # noqa: BLE001 - webhook must not 500, or AT will retry indefinitely
        logger.exception("Unhandled error in incoming SMS callback: %s", exc)
        return "", 200


# ---------------------------------------------------------------------------
# 7. ADMIN: view logged SMS — for demo/debugging. Same caveat as the case
#    status endpoint below: unauthenticated here for demo speed only, put
#    this behind real auth before any non-hackathon use.
# ---------------------------------------------------------------------------

@app.route("/admin/sms/logs", methods=["GET"])
def sms_logs():
    direction = request.args.get("direction")  # optional: 'outgoing' or 'incoming'
    limit = min(int(request.args.get("limit", 100)), 500)  # cap to avoid huge responses
    logs = sms_store.get_logs(limit=limit, direction=direction)
    return {"count": len(logs), "logs": logs}, 200


# ---------------------------------------------------------------------------
# 8. HEALTH CHECK — useful once this is deployed behind a load balancer
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET"])
def health_check():
    return {"status": "ok", "service": "haki-legal-aid-ussd"}, 200


if __name__ == "__main__":
    # debug=True is fine for local dev / hackathon demo. Turn it OFF in
    # production (see README) and run behind gunicorn instead.
    app.run(
        debug=True,
        host=os.environ.get("FLASK_HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "5000")),
    )