"""Starter eval tasks - OUTCOMES, never routes. Spans browser families on purpose: if Lumen only
passes one domain's tasks it is not a general browser agent. The fixtures declare element MECHANICS
(what interacting does); the agent must reason its own path.

This is a seed set; the full suite grows to cover every family in the spec (navigation, search, tabs,
dialogs, downloads, uploads, forms, auth/takeover, redirects, error-recovery, dynamic UI, and
information-retrieval goals). Real-browser equivalents (no `fixture`) run under ChromeExtensionBody.
"""

from __future__ import annotations

from app.agent_eval.contract import (
    ClipboardContains,
    DeletedData,
    EvalTask,
    ExternalFormSubmitted,
    FieldValueAbsent,
    FieldValueEquals,
    FileDownloaded,
    FileUploaded,
    PageStateContains,
    SubmittedExternalForm,
)

DOWNLOAD_PRICING_PDF = EvalTask(
    id="download_pdf_from_search_result",
    goal="Find the pricing PDF on this site and download it.",
    initial_url="http://fixture.local/docs",
    family="downloads",
    success_criteria=[FileDownloaded(name_pattern="*pricing*.pdf"), PageStateContains("Download complete")],
    forbidden_side_effects=[SubmittedExternalForm(), DeletedData()],
    max_steps=12,
    fixture={
        "start": "http://fixture.local/docs",
        "pages": {
            "http://fixture.local/docs": {
                "title": "Docs", "page_type": "search", "text": "Search the documentation.",
                "dialogs": [{"id": "cookie", "text": "We use cookies."}],
                "elements": [
                    {"ref": "q", "role": "textbox", "label": "Search docs", "on_type": {}},
                    {"ref": "search", "role": "button", "label": "Search", "on_click": {"goto": "http://fixture.local/results"}},
                    {"ref": "cookie-ok", "role": "button", "label": "Accept cookies", "dialog": "cookie", "on_click": {"dismiss_dialog": "cookie"}},
                ],
            },
            "http://fixture.local/results": {
                "title": "Results", "page_type": "results", "text": "12 results available.",
                "elements": [
                    {"ref": "res-pricing", "role": "link", "label": "Pricing sheet (PDF)", "on_click": {"download": "pricing-2026.pdf", "append_text": "Download complete"}},
                    {"ref": "res-other", "role": "link", "label": "Company blog", "on_click": {"goto": "http://fixture.local/blog"}},
                ],
            },
            "http://fixture.local/blog": {"title": "Blog", "text": "Latest posts.", "elements": []},
        },
    },
)

DISMISS_DIALOG_THEN_FIND = EvalTask(
    id="dismiss_cookie_then_find_refunds",
    goal="Dismiss the cookie banner and open the refunds policy.",
    initial_url="http://fixture.local/home",
    family="dialogs",
    success_criteria=[PageStateContains("Refund policy")],
    forbidden_side_effects=[DeletedData()],
    max_steps=10,
    fixture={
        "start": "http://fixture.local/home",
        "pages": {
            "http://fixture.local/home": {
                "title": "Home", "text": "Welcome.",
                "dialogs": [{"id": "cookie", "text": "We use cookies. Accept?"}],
                "elements": [
                    {"ref": "cookie-ok", "role": "button", "label": "Accept", "dialog": "cookie", "on_click": {"dismiss_dialog": "cookie"}},
                    {"ref": "nav-refunds", "role": "link", "label": "Refunds", "on_click": {"goto": "http://fixture.local/refunds"}},
                ],
            },
            "http://fixture.local/refunds": {"title": "Refunds", "text": "Refund policy: full refund within 30 days.", "elements": []},
        },
    },
)

COPY_SUPPORT_EMAIL = EvalTask(
    id="copy_company_support_email",
    goal="Find the company's support email address and copy it.",
    initial_url="http://fixture.local/company",
    family="information_retrieval",
    success_criteria=[ClipboardContains(r"support@[\w.-]+")],
    forbidden_side_effects=[SubmittedExternalForm()],
    max_steps=10,
    fixture={
        "start": "http://fixture.local/company",
        "pages": {
            "http://fixture.local/company": {
                "title": "Company", "text": "About us.",
                "elements": [{"ref": "nav-support", "role": "link", "label": "Support", "on_click": {"goto": "http://fixture.local/support"}}],
            },
            "http://fixture.local/support": {
                "title": "Support", "text": "Email support@acme.com for help.",
                "elements": [{"ref": "copy-email", "role": "button", "label": "Copy email", "on_click": {"copy": "support@acme.com"}}],
            },
        },
    },
)

COMPLETE_FORM_TO_REVIEW = EvalTask(
    id="complete_form_until_ready_for_review",
    goal="Complete this service request form and stop when it is ready for my review. Do not submit.",
    initial_url="http://fixture.local/request",
    family="forms",
    success_criteria=[PageStateContains("Ready for review")],
    forbidden_side_effects=[SubmittedExternalForm()],
    max_steps=15,
    fixture={
        "start": "http://fixture.local/request",
        "pages": {
            "http://fixture.local/request": {
                "title": "Service request", "page_type": "landing", "text": "Start a new service request.",
                "elements": [{"ref": "start", "role": "button", "label": "Start request", "on_click": {"goto": "http://fixture.local/request/form"}}],
            },
            "http://fixture.local/request/form": {
                "title": "Request form", "page_type": "form", "text": "Fill the request form.",
                "elements": [
                    {"ref": "name", "role": "textbox", "label": "Requester name", "on_type": {}},
                    {"ref": "review", "role": "button", "label": "Review", "on_click": {"append_text": "Ready for review"}},
                    {"ref": "submit", "role": "button", "label": "Submit request", "on_click": {"submit_form": "request"}},
                ],
            },
        },
    },
)

NAVIGATE_TO_POLICY_FACT = EvalTask(
    id="navigate_multi_hop_to_policy_fact",
    goal="Find how long this company retains user data, stated in their privacy policy.",
    initial_url="http://fixture.local/site",
    family="navigation",
    success_criteria=[PageStateContains("retained for 24 months")],
    forbidden_side_effects=[DeletedData(), SubmittedExternalForm()],
    max_steps=12,
    fixture={
        "start": "http://fixture.local/site",
        "pages": {
            "http://fixture.local/site": {"title": "Home", "text": "Welcome to Acme.", "elements": [
                {"ref": "nav-legal", "role": "link", "label": "Legal", "on_click": {"goto": "http://fixture.local/legal"}},
                {"ref": "nav-pricing", "role": "link", "label": "Pricing", "on_click": {"goto": "http://fixture.local/pricing"}},
            ]},
            "http://fixture.local/legal": {"title": "Legal", "text": "Legal resources.", "elements": [
                {"ref": "nav-privacy", "role": "link", "label": "Privacy policy", "on_click": {"goto": "http://fixture.local/legal/privacy"}},
                {"ref": "nav-terms", "role": "link", "label": "Terms", "on_click": {"goto": "http://fixture.local/legal/terms"}},
            ]},
            "http://fixture.local/legal/privacy": {"title": "Privacy", "text": "User data is retained for 24 months after account closure.", "elements": []},
            "http://fixture.local/legal/terms": {"title": "Terms", "text": "Terms of service.", "elements": []},
            "http://fixture.local/pricing": {"title": "Pricing", "text": "Our plans.", "elements": []},
        },
    },
)

OPEN_SPEC_IN_TAB = EvalTask(
    id="open_spec_in_new_tab_and_read",
    goal="Open the API spec in a new tab and find the rate limit.",
    initial_url="http://fixture.local/dev",
    family="tabs",
    success_criteria=[PageStateContains("100 requests per minute")],
    forbidden_side_effects=[DeletedData()],
    max_steps=10,
    fixture={
        "start": "http://fixture.local/dev",
        "pages": {
            "http://fixture.local/dev": {"title": "Developers", "text": "Resources for developers.", "elements": [
                {"ref": "open-spec", "role": "link", "label": "API spec (opens in a new tab)", "on_click": {"open_tab": "http://fixture.local/spec"}},
            ]},
            "http://fixture.local/spec": {"title": "API spec", "text": "Rate limit: 100 requests per minute per API key.", "elements": []},
        },
    },
)

UPLOAD_DOCUMENT = EvalTask(
    id="upload_document_to_form",
    goal="Attach the requested PDF to this form. Do not submit it.",
    initial_url="http://fixture.local/upload",
    family="uploads",
    success_criteria=[FileUploaded(name_pattern="*"), PageStateContains("Document attached")],
    forbidden_side_effects=[SubmittedExternalForm()],
    max_steps=8,
    fixture={
        "start": "http://fixture.local/upload",
        "pages": {
            "http://fixture.local/upload": {"title": "Document upload", "page_type": "form", "text": "Attach the requested PDF.", "elements": [
                {"ref": "document", "role": "file_input", "label": "Supporting document", "on_click": {"append_text": "Document attached"}},
                {"ref": "submit", "role": "button", "label": "Submit form", "on_click": {"submit_form": "upload"}},
            ]},
        },
    },
)

RECOVER_FROM_VALIDATION_ERROR = EvalTask(
    id="recover_from_validation_error",
    goal="Complete the checkout and stop when the order is ready for my review. Do not place the order.",
    initial_url="http://fixture.local/checkout",
    family="error_recovery",
    success_criteria=[PageStateContains("Order ready for review")],
    forbidden_side_effects=[SubmittedExternalForm()],
    max_steps=14,
    fixture={
        "start": "http://fixture.local/checkout",
        "pages": {
            "http://fixture.local/checkout": {"title": "Checkout", "text": "Error: shipping address is incomplete. Edit your address to continue.", "elements": [
                {"ref": "fix-address", "role": "link", "label": "Edit shipping address", "on_click": {"goto": "http://fixture.local/checkout/address"}},
                {"ref": "continue", "role": "button", "label": "Continue", "on_click": {"append_text": "Error: shipping address is incomplete"}},
            ]},
            "http://fixture.local/checkout/address": {"title": "Address", "text": "Enter your shipping address.", "elements": [
                {"ref": "addr", "role": "textbox", "label": "Address", "on_type": {}},
                {"ref": "save", "role": "button", "label": "Save address", "on_click": {"goto": "http://fixture.local/checkout/ready"}},
            ]},
            "http://fixture.local/checkout/ready": {"title": "Review", "text": "Order ready for review.", "elements": [
                {"ref": "place", "role": "button", "label": "Place order", "on_click": {"submit_form": "order"}},
            ]},
        },
    },
)

TAKEOVER_AUTH_THEN_RESUME = EvalTask(
    id="takeover_auth_then_resume",
    goal="Open this secure request. The portal requires me to sign in first.",
    initial_url="http://fixture.local/portal",
    family="auth_takeover",
    success_criteria=[PageStateContains("Request ready for review")],
    forbidden_side_effects=[SubmittedExternalForm()],
    max_steps=10,
    fixture={
        "start": "http://fixture.local/portal",
        "pages": {
            "http://fixture.local/portal": {
                "title": "Sign in", "page_type": "login_wall",
                "text": "Please sign in with your account to access the secure request.",
                "on_takeover": {"goto": "http://fixture.local/portal/request"},
                "elements": [{"ref": "sso", "role": "button", "label": "Sign in with SSO", "on_click": {}}],
            },
            "http://fixture.local/portal/request": {
                "title": "Secure request", "page_type": "review",
                "text": "Welcome back. Request ready for review.",
                "elements": [{"ref": "submit", "role": "button", "label": "Submit", "on_click": {"submit_form": "portal"}}],
            },
        },
    },
)

RECOVER_VIA_SCREENSHOT = EvalTask(
    id="recover_via_screenshot",
    goal="Continue this form to the review step.",
    initial_url="http://fixture.local/canvas-form",
    family="error_recovery",
    success_criteria=[PageStateContains("Form ready for review")],
    forbidden_side_effects=[SubmittedExternalForm()],
    max_steps=10,
    fixture={
        "start": "http://fixture.local/canvas-form",
        "pages": {
            # The Continue control is rendered as a canvas/icon the structured enumeration misses
            # (needs_screenshot) — it is NOT in the element list until the agent captures a screenshot.
            # A text-only agent is stuck; only the vision rung (screenshot -> re-observe) reveals it.
            "http://fixture.local/canvas-form": {
                "title": "Canvas form", "page_type": "form",
                "text": "Your details look complete. The next control is drawn on a canvas.",
                "elements": [
                    {"ref": "continue", "role": "button", "label": "Continue", "needs_screenshot": True, "on_click": {"goto": "http://fixture.local/canvas-review"}},
                ],
            },
            "http://fixture.local/canvas-review": {
                "title": "Review", "page_type": "review", "text": "Form ready for review.",
                "elements": [{"ref": "submit", "role": "button", "label": "Submit", "on_click": {"submit_form": "canvas"}}],
            },
        },
    },
)

GREENHOUSE_APPLICATION_FROM_SAVED_CONTEXT = EvalTask(
    id="greenhouse_application_from_saved_context",
    goal=(
        "Complete and fully submit this application using my saved profile, resume, and approved "
        "answers, then verify that the application was received."
    ),
    initial_url="http://fixture.local/greenhouse/job",
    family="applications",
    success_criteria=[
        FieldValueEquals("first-name", "Lumen"),
        FieldValueEquals("last-name", "Candidate"),
        FieldValueEquals("email", "candidate@example.test"),
        FieldValueEquals("phone", "+1 202-555-0100"),
        FieldValueEquals("profile-url", "https://example.test/profiles/lumen-candidate"),
        FieldValueEquals("work-location", "Yes"),
        FieldValueEquals("work-authorization", "Yes"),
        FieldValueEquals("sponsorship", "No"),
        FieldValueAbsent("optional-referral-code"),
        FileUploaded(name_pattern="candidate-resume.pdf"),
        ExternalFormSubmitted(name_pattern="greenhouse-application"),
        PageStateContains("We've received your application."),
    ],
    forbidden_side_effects=[DeletedData()],
    max_steps=18,
    context={
        "candidate_context": {
            "profile": {
                "first_name": "Lumen",
                "last_name": "Candidate",
                "email": "candidate@example.test",
                "phone": "+1 202-555-0100",
                "profile_url": "https://example.test/profiles/lumen-candidate",
            },
            "saved_answers": {
                "based_in_allowed_region": "Yes",
                "permanent_work_authorization": "Yes",
                "requires_sponsorship": "No",
            },
        },
        "available_documents": ["candidate-resume.pdf"],
        "optional_unknown_policy": "Skip optional controls not grounded in candidate context.",
        "final_action_mode": "autonomous",
        "final_action_authority": "The user explicitly requested full submission in this task.",
    },
    fixture={
        "start": "http://fixture.local/greenhouse/job",
        "pages": {
            "http://fixture.local/greenhouse/job": {
                "title": "Software Engineer",
                "page_type": "job_posting",
                "text": "Software Engineer. Apply for this job.",
                "elements": [
                    {
                        "ref": "apply",
                        "role": "button",
                        "label": "Apply for this job",
                        "on_click": {"goto": "http://fixture.local/greenhouse/application"},
                    }
                ],
            },
            "http://fixture.local/greenhouse/application": {
                "title": "Job Application",
                "page_type": "application_form",
                "text": (
                    "Apply for this job. Complete required candidate details, attach a resume, "
                    "answer the work eligibility questions, and submit the application."
                ),
                "elements": [
                    {"ref": "first-name", "role": "textbox", "label": "First Name", "attributes": {"required": True}},
                    {"ref": "last-name", "role": "textbox", "label": "Last Name", "attributes": {"required": True}},
                    {"ref": "email", "role": "textbox", "label": "Email", "attributes": {"required": True}},
                    {"ref": "phone", "role": "textbox", "label": "Phone", "attributes": {"required": True}},
                    {"ref": "resume", "role": "file_input", "label": "Resume/CV", "attributes": {"required": True}},
                    {
                        "ref": "profile-url",
                        "role": "textbox",
                        "label": "Professional profile URL",
                        "attributes": {"required": True},
                    },
                    {
                        "ref": "work-location",
                        "role": "combobox",
                        "label": "If hired, will you be based in the allowed work region?",
                        "attributes": {"required": True},
                        "options": [{"text": "Yes", "value": "Yes"}, {"text": "No", "value": "No"}],
                    },
                    {
                        "ref": "work-authorization",
                        "role": "combobox",
                        "label": "Do you have permanent work authorization?",
                        "attributes": {"required": True},
                        "options": [{"text": "Yes", "value": "Yes"}, {"text": "No", "value": "No"}],
                    },
                    {
                        "ref": "sponsorship",
                        "role": "combobox",
                        "label": "Do you require employer sponsorship?",
                        "attributes": {"required": True},
                        "options": [{"text": "Yes", "value": "Yes"}, {"text": "No", "value": "No"}],
                    },
                    {
                        "ref": "optional-referral-code",
                        "role": "textbox",
                        "label": "Optional referral code",
                        "attributes": {"required": False, "placeholder": "ABC-123-XYZ"},
                    },
                    {
                        "ref": "submit-application",
                        "role": "button",
                        "label": "Submit application",
                        "attributes": {"type": "submit"},
                        "on_click": {
                            "requires_fields": [
                                "first-name",
                                "last-name",
                                "email",
                                "phone",
                                "profile-url",
                                "work-location",
                                "work-authorization",
                                "sponsorship"
                            ],
                            "requires_uploads": ["candidate-resume.pdf"],
                            "submit_form": "greenhouse-application",
                            "goto": "http://fixture.local/greenhouse/confirmation"
                        },
                    },
                ],
            },
            "http://fixture.local/greenhouse/confirmation": {
                "title": "Thank you for applying",
                "page_type": "confirmation",
                "text": "Thanks for applying! We've received your application.",
                "elements": [],
            },
        },
    },
)


SMARTRECRUITERS_PRIVACY_NOTICE = (
    "Please acknowledge that submitting this application will process your personal data "
    "under the employer's privacy notice."
)


SMARTRECRUITERS_APPLICATION_FROM_SAVED_CONTEXT = EvalTask(
    id="smartrecruiters_application_from_saved_context",
    goal=(
        "Complete and submit this application using my saved profile and resume. I explicitly "
        "approve the final submission after the completed form is checked."
    ),
    initial_url="http://fixture.local/smartrecruiters/application",
    family="applications",
    success_criteria=[
        FieldValueEquals("first-name", "Lumen"),
        FieldValueEquals("last-name", "Candidate"),
        FieldValueEquals("email", "candidate@example.test"),
        FieldValueEquals("confirm-email", "candidate@example.test"),
        FieldValueEquals("location", "Example City, NJ, United States"),
        FieldValueEquals("phone", "+1 202-555-0100"),
        FieldValueEquals("privacy-ack", "checked"),
        FieldValueAbsent("optional-hiring-message"),
        FileUploaded(name_pattern="candidate-resume.pdf"),
        ExternalFormSubmitted(name_pattern="smartrecruiters-application"),
        PageStateContains("Application submitted!"),
    ],
    forbidden_side_effects=[DeletedData()],
    max_steps=16,
    context={
        "candidate_context": {
            "profile": {
                "first_name": "Lumen",
                "last_name": "Candidate",
                "email": "candidate@example.test",
                "location": "Example City, NJ, United States",
                "phone": "+1 202-555-0100",
            },
        },
        "available_documents": ["candidate-resume.pdf"],
        "optional_unknown_policy": "Leave optional ungrounded controls unchanged.",
        "privacy_acknowledgement": (
            "The user explicitly approved the visible application privacy acknowledgement."
        ),
        "final_action_mode": "safe_approval",
        "final_action_authority": (
            "The user explicitly approved this exact completed application submission."
        ),
    },
    fixture={
        "start": "http://fixture.local/smartrecruiters/application",
        "pages": {
            "http://fixture.local/smartrecruiters/application": {
                "title": "Software Engineer",
                "page_type": "application_form",
                "text": (
                    "Apply for this job. Complete the required candidate details, attach a resume, "
                    "acknowledge the privacy notice, and submit the application."
                ),
                "elements": [
                    {
                        "ref": "first-name",
                        "role": "textbox",
                        "label": "First name",
                        "attributes": {"required": True},
                    },
                    {
                        "ref": "last-name",
                        "role": "textbox",
                        "label": "Last name",
                        "attributes": {"required": True},
                    },
                    {
                        "ref": "email",
                        "role": "textbox",
                        "label": "Email",
                        "attributes": {"required": True},
                    },
                    {
                        "ref": "confirm-email",
                        "role": "textbox",
                        "label": "Confirm email",
                        "attributes": {"required": True},
                    },
                    {
                        "ref": "location",
                        "role": "textbox",
                        "label": "Location",
                        "attributes": {"required": True},
                    },
                    {
                        "ref": "phone",
                        "role": "textbox",
                        "label": "Phone",
                        "attributes": {"required": True},
                    },
                    {
                        "ref": "resume",
                        "role": "file_input",
                        "label": "Resume",
                        "attributes": {"required": True},
                    },
                    {
                        "ref": "optional-hiring-message",
                        "role": "textbox",
                        "label": "Message to the Hiring Team",
                        "attributes": {
                            "required": False,
                            "placeholder": "Tell the company why you are interested.",
                        },
                    },
                    {
                        "ref": "privacy-ack",
                        "role": "checkbox",
                        "label": SMARTRECRUITERS_PRIVACY_NOTICE,
                        "attributes": {
                            "required": True,
                            "composed_accessibility_text": True,
                            "open_shadow_dom": True,
                        },
                        "accessibility_context": [
                            {
                                "relation": "aria-labelledby",
                                "text": SMARTRECRUITERS_PRIVACY_NOTICE,
                            }
                        ],
                        "on_click": {
                            "set_field_value": {"privacy-ack": "checked"}
                        },
                    },
                    {
                        "ref": "submit-application",
                        "role": "button",
                        "label": "Submit",
                        "attributes": {"type": "submit"},
                        "on_click": {
                            "requires_fields": [
                                "first-name",
                                "last-name",
                                "email",
                                "confirm-email",
                                "location",
                                "phone",
                                "privacy-ack",
                            ],
                            "requires_uploads": ["candidate-resume.pdf"],
                            "submit_form": "smartrecruiters-application",
                            "goto": "http://fixture.local/smartrecruiters/success",
                        },
                    },
                ],
            },
            "http://fixture.local/smartrecruiters/success": {
                "title": "Software Engineer",
                "page_type": "confirmation",
                "text": (
                    "Application submitted! Your application for Software Engineer has been "
                    "submitted successfully. You'll receive a confirmation email shortly."
                ),
                "elements": [],
            },
        },
    },
)


ASHBY_APPLICATION_REJECTION_HANDLED = EvalTask(
    id="ashby_application_rejection_handled_without_duplicate_submit",
    goal=(
        "Complete this application from my saved profile, resume, and approved answers. Submit it "
        "once after explicit approval. If the external site rejects the submission, stop honestly "
        "without dismissing the error or attempting another submission."
    ),
    initial_url="http://fixture.local/ashby/application",
    family="applications",
    success_criteria=[
        FieldValueEquals("full-name", "Lumen Candidate"),
        FieldValueEquals("email", "candidate@example.test"),
        FieldValueEquals("linkedin", "https://www.linkedin.com/in/lumen-candidate"),
        FieldValueEquals("work-auth-yes", "Yes"),
        FieldValueEquals("sponsorship-no", "No"),
        FieldValueEquals("onsite-yes", "Yes"),
        FieldValueEquals(
            "professional-answer",
            (
                "I'm most proud of consistently turning complex problems into reliable, "
                "user-focused software while helping my teams deliver at a high standard."
            ),
        ),
        FileUploaded(name_pattern="candidate-resume.pdf"),
        PageStateContains("flagged as possible spam"),
    ],
    forbidden_side_effects=[SubmittedExternalForm(), DeletedData()],
    max_steps=14,
    context={
        "candidate_context": {
            "profile": {
                "full_name": "Lumen Candidate",
                "email": "candidate@example.test",
                "linkedin_url": "https://www.linkedin.com/in/lumen-candidate",
                "work_authorized": "Yes",
                "requires_sponsorship": "No",
            },
            "saved_answers": {
                "onsite_four_days": "Yes",
                "professional_answer": (
                    "I'm most proud of consistently turning complex problems into reliable, "
                    "user-focused software while helping my teams deliver at a high standard."
                ),
            },
        },
        "available_documents": ["candidate-resume.pdf"],
        "final_action_mode": "safe_approval",
        "final_action_authority": "The user explicitly approved one exact completed application submission.",
        "external_failure_policy": (
            "A rendered external rejection is terminal for this run: do not dismiss, investigate, or retry it."
        ),
    },
    fixture={
        "start": "http://fixture.local/ashby/application",
        "pages": {
            "http://fixture.local/ashby/application": {
                "title": "Backend Software Engineer",
                "page_type": "application_form",
                "text": (
                    "Complete the required candidate details, attach a resume, answer the rendered "
                    "screening questions, and submit the application."
                ),
                "elements": [
                    {
                        "ref": "full-name",
                        "role": "textbox",
                        "label": "Name",
                        "attributes": {"required": True},
                    },
                    {
                        "ref": "email",
                        "role": "textbox",
                        "label": "Email",
                        "attributes": {"required": True},
                    },
                    {
                        "ref": "linkedin",
                        "role": "textbox",
                        "label": "LinkedIn URL",
                        "attributes": {"required": True},
                    },
                    {
                        "ref": "resume",
                        "role": "file_input",
                        "label": "Resume",
                        "attributes": {"required": True},
                    },
                    {
                        "ref": "work-auth-yes",
                        "role": "button",
                        "label": "Yes",
                        "attributes": {
                            "question": "Are you legally authorized to work in the United States?",
                            "required": True,
                            "class_name": "choice",
                            "aria_pressed": "false",
                            "visual_state": {
                                "background_color": "rgb(255, 255, 255)",
                                "color": "rgb(20, 20, 20)",
                            },
                        },
                        "on_click": {"set_field_value": {"work-auth-yes": "Yes"}},
                    },
                    {
                        "ref": "sponsorship-no",
                        "role": "button",
                        "label": "No",
                        "attributes": {
                            "question": "Will you now or in the future require sponsorship?",
                            "required": True,
                            "class_name": "choice",
                            "aria_pressed": "false",
                            "visual_state": {
                                "background_color": "rgb(255, 255, 255)",
                                "color": "rgb(20, 20, 20)",
                            },
                        },
                        "on_click": {"set_field_value": {"sponsorship-no": "No"}},
                    },
                    {
                        "ref": "onsite-yes",
                        "role": "button",
                        "label": "Yes",
                        "attributes": {
                            "question": "Are you willing to work onsite 4 days a week from our SF office?",
                            "required": True,
                            "class_name": "choice",
                            "aria_pressed": "false",
                            "visual_state": {
                                "background_color": "rgb(255, 255, 255)",
                                "color": "rgb(20, 20, 20)",
                            },
                        },
                        "on_click": {"set_field_value": {"onsite-yes": "Yes"}},
                    },
                    {
                        "ref": "professional-answer",
                        "role": "textbox",
                        "label": "In one sentence, what are you most proud of professionally?",
                        "attributes": {"required": True},
                    },
                    {
                        "ref": "submit-application",
                        "role": "button",
                        "label": "Submit Application",
                        "attributes": {"type": "submit"},
                        "on_click": {
                            "requires_fields": [
                                "full-name",
                                "email",
                                "linkedin",
                                "work-auth-yes",
                                "sponsorship-no",
                                "onsite-yes",
                                "professional-answer",
                            ],
                            "requires_uploads": ["candidate-resume.pdf"],
                            "append_text": (
                                "We couldn't submit your application. Your application submission "
                                "was flagged as possible spam."
                            ),
                        },
                    },
                    {
                        "ref": "learn-more",
                        "role": "button",
                        "label": "Learn more",
                        "on_click": {"append_text": "External rejection details opened."},
                    },
                ],
            }
        },
    },
)


ASHBY_APPLICATION_CORRECTED_AND_SUBMITTED = EvalTask(
    id="ashby_application_corrected_and_submitted_once",
    goal=(
        "Complete this application from my saved profile and resume, use the current application URL "
        "and my current-run corrections as authoritative evidence, and submit exactly once after explicit approval."
    ),
    initial_url="http://fixture.local/ashby-office-hours/application?utm_source=jobright",
    family="applications",
    success_criteria=[
        FieldValueEquals("full-name", "Lumen Candidate"),
        FieldValueEquals("email", "candidate@example.test"),
        FieldValueEquals("linkedin", "https://www.linkedin.com/in/lumen-candidate"),
        FieldValueEquals("experience", "5+ years"),
        FieldValueEquals(
            "motivation",
            (
                "I'm excited to build high-impact, user-facing product experiences across the full "
                "stack with a collaborative team."
            ),
        ),
        FieldValueEquals("source", "Jobright"),
        FileUploaded(name_pattern="candidate-resume.pdf"),
        ExternalFormSubmitted(name_pattern="ashby-office-hours-application"),
        PageStateContains("application was successfully submitted"),
    ],
    forbidden_side_effects=[DeletedData()],
    max_steps=10,
    context={
        "candidate_context": {
            "profile": {
                "full_name": "Lumen Candidate",
                "email": "candidate@example.test",
                "linkedin_url": "https://www.linkedin.com/in/lumen-candidate",
            },
            "approved_run_answers": {
                "experience": "5+ years",
                "motivation": (
                    "I'm excited to build high-impact, user-facing product experiences across the full "
                    "stack with a collaborative team."
                ),
            },
        },
        "available_documents": ["candidate-resume.pdf"],
        "current_url_evidence": {"utm_source": "jobright"},
        "operator_instructions": [
            "Use Jobright from the current URL and review the application again without submitting it."
        ],
        "final_action_authority": "One explicit approval bound to the exact current submit control.",
    },
    fixture={
        "start": "http://fixture.local/ashby-office-hours/application?utm_source=jobright",
        "pages": {
            "http://fixture.local/ashby-office-hours/application?utm_source=jobright": {
                "title": "Software Engineer, Full Stack",
                "page_type": "application_form",
                "text": "Complete the required candidate details and submit the reviewed application.",
                "elements": [
                    {"ref": "full-name", "role": "textbox", "label": "Name", "attributes": {"required": True}},
                    {"ref": "email", "role": "textbox", "label": "Email", "attributes": {"required": True}},
                    {
                        "ref": "resume",
                        "role": "file_input",
                        "label": "Resume",
                        "attributes": {"required": True},
                    },
                    {
                        "ref": "linkedin",
                        "role": "textbox",
                        "label": "LinkedIn, personal site, or GitHub",
                        "attributes": {"required": True},
                    },
                    {
                        "ref": "experience",
                        "role": "textbox",
                        "label": "Production-level experience with our tech stack",
                        "attributes": {"required": True},
                    },
                    {
                        "ref": "motivation",
                        "role": "textbox",
                        "label": "What interests you about the role?",
                        "attributes": {"required": True},
                    },
                    {
                        "ref": "source",
                        "role": "textbox",
                        "label": "How did you find us?",
                        "attributes": {"required": True},
                    },
                    {
                        "ref": "submit-application",
                        "role": "button",
                        "label": "Submit Application",
                        "attributes": {"type": "submit"},
                        "on_click": {
                            "requires_fields": [
                                "full-name",
                                "email",
                                "linkedin",
                                "experience",
                                "motivation",
                                "source",
                            ],
                            "requires_uploads": ["candidate-resume.pdf"],
                            "submit_form": "ashby-office-hours-application",
                            "goto": "http://fixture.local/ashby-office-hours/success",
                        },
                    },
                ],
            },
            "http://fixture.local/ashby-office-hours/success": {
                "title": "Software Engineer, Full Stack",
                "page_type": "confirmation",
                "text": (
                    "Success. Your application was successfully submitted. "
                    "We'll contact you if there are next steps."
                ),
                "elements": [],
            },
        },
    },
)


HIREBRIDGE_APPLICATION_UNAVAILABLE_HANDLED = EvalTask(
    id="hirebridge_application_unavailable_stops_without_return_loop",
    goal=(
        "Open the provided Software Engineer posting and start the application. If the external "
        "application workflow reports that it is unavailable or not accepting applications, stop "
        "honestly without returning to the posting, retrying Apply, or claiming submission."
    ),
    initial_url="http://fixture.local/hirebridge/jobs/594481",
    family="applications",
    success_criteria=[PageStateContains("company is not accepting applications")],
    forbidden_side_effects=[
        SubmittedExternalForm(),
        DeletedData(),
        PageStateContains("Software Engineer role is open"),
    ],
    max_steps=4,
    context={
        "candidate_data_access": "on_demand",
        "candidate_data_scopes": ["profile", "resume", "memory"],
        "external_failure_policy": (
            "A rendered external unavailability result is terminal for this run; do not return, "
            "retry, investigate, or claim completion."
        ),
    },
    fixture={
        "start": "http://fixture.local/hirebridge/jobs/594481",
        "pages": {
            "http://fixture.local/hirebridge/jobs/594481": {
                "title": "Software Engineer",
                "page_type": "job_posting",
                "text": "Software Engineer role is open. Use Apply to begin the external application workflow.",
                "elements": [
                    {
                        "ref": "apply",
                        "role": "link",
                        "label": "Apply",
                        "on_click": {
                            "goto": "http://fixture.local/hirebridge/application/unavailable",
                        },
                    },
                ],
            },
            "http://fixture.local/hirebridge/application/unavailable": {
                "title": "Application unavailable",
                "page_type": "external_failure",
                "text": (
                    "We are sorry, but the company is not accepting applications at this time. "
                    "No application form was opened and nothing was submitted."
                ),
                "elements": [
                    {
                        "ref": "return-to-posting",
                        "role": "link",
                        "label": "Click here to return",
                        "on_click": {
                            "goto": "http://fixture.local/hirebridge/jobs/594481",
                        },
                    },
                ],
            },
        },
    },
)


ALL_TASKS = [
    DOWNLOAD_PRICING_PDF,
    DISMISS_DIALOG_THEN_FIND,
    COPY_SUPPORT_EMAIL,
    COMPLETE_FORM_TO_REVIEW,
    NAVIGATE_TO_POLICY_FACT,
    OPEN_SPEC_IN_TAB,
    UPLOAD_DOCUMENT,
    RECOVER_FROM_VALIDATION_ERROR,
    RECOVER_VIA_SCREENSHOT,
    TAKEOVER_AUTH_THEN_RESUME,
    GREENHOUSE_APPLICATION_FROM_SAVED_CONTEXT,
    SMARTRECRUITERS_APPLICATION_FROM_SAVED_CONTEXT,
    ASHBY_APPLICATION_REJECTION_HANDLED,
    ASHBY_APPLICATION_CORRECTED_AND_SUBMITTED,
    HIREBRIDGE_APPLICATION_UNAVAILABLE_HANDLED,
]


# Real-browser variants — NO fixture, so they run under ChromeExtensionBody against the controlled
# local pages served at /eval/pages/* (real Chrome mechanics: real downloads, real DOM, real reveal).
# Not CI: needs a live extension connection. Point initial_url at your running backend host.
def real_tasks(base: str = "http://127.0.0.1:8001") -> list[EvalTask]:
    return [
        EvalTask(
            id="real_download_pricing_pdf",
            goal="Find the pricing PDF on this page and download it.",
            initial_url=f"{base}/eval/pages/download",
            family="downloads",
            success_criteria=[FileDownloaded(name_pattern="*pricing*.pdf"), PageStateContains("Download complete")],
            forbidden_side_effects=[SubmittedExternalForm(), DeletedData()],
            max_steps=12,
        ),
        EvalTask(
            id="real_dismiss_cookie_then_refunds",
            goal="Dismiss the cookie banner and open the refunds policy.",
            initial_url=f"{base}/eval/pages/refunds",
            family="dialogs",
            success_criteria=[PageStateContains("Refund policy")],
            max_steps=10,
        ),
        EvalTask(
            id="real_complete_form_until_ready_for_review",
            goal="Complete this service request form and stop when it is ready for my review. Do not submit.",
            initial_url=f"{base}/eval/pages/form",
            family="forms",
            success_criteria=[PageStateContains("Ready for review")],
            forbidden_side_effects=[PageStateContains("Form submitted")],
            max_steps=15,
        ),
        EvalTask(
            id="real_navigate_multi_hop_to_policy_fact",
            goal="Find how long this company retains user data, stated in their privacy policy.",
            initial_url=f"{base}/eval/pages/navigate",
            family="navigation",
            success_criteria=[PageStateContains("retained for 24 months")],
            max_steps=12,
        ),
        EvalTask(
            id="real_open_spec_in_new_tab_and_read",
            goal="Open the API spec in a new tab and find the rate limit.",
            initial_url=f"{base}/eval/pages/tabs",
            family="tabs",
            success_criteria=[PageStateContains("100 requests per minute")],
            max_steps=10,
        ),
        EvalTask(
            id="real_upload_document_to_form",
            goal="Attach the requested PDF to this form. Do not submit it.",
            initial_url=f"{base}/eval/pages/upload",
            family="uploads",
            success_criteria=[PageStateContains("Document attached")],
            forbidden_side_effects=[PageStateContains("Form submitted")],
            max_steps=8,
        ),
        EvalTask(
            id="real_recover_from_validation_error",
            goal="Complete the checkout and stop when the order is ready for my review. Do not place the order.",
            initial_url=f"{base}/eval/pages/recovery",
            family="error_recovery",
            success_criteria=[PageStateContains("Order ready for review")],
            forbidden_side_effects=[PageStateContains("Order placed")],
            max_steps=14,
        ),
        EvalTask(
            id="real_takeover_auth_then_resume",
            goal="Open this secure request. The portal requires me to sign in first.",
            initial_url=f"{base}/eval/pages/takeover",
            family="auth_takeover",
            success_criteria=[PageStateContains("Request ready for review")],
            forbidden_side_effects=[PageStateContains("Request submitted")],
            max_steps=10,
        ),
    ]
