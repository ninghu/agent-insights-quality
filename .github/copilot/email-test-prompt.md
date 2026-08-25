# Controlled HTML email test

Send exactly one HTML test email to the recipient configured by the repository. Do not run Agent
traffic and do not modify the repository.

Use the available Copilot email capability directly. Set HTML mode explicitly. Do not create a draft.
Do not retry an ambiguous send.

The HTML must contain a navy title bar and one styled two-column table using inline Outlook-safe
styles. If delivery is confirmed, return `SENT - <subject>`. On explicit no-send failure, return the
exact error. On an ambiguous result, return `SEND STATUS UNKNOWN - manual verification required`.

