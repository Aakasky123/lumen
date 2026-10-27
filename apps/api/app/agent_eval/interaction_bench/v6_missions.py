"""Interaction Gauntlet generation 6 — web-app workflows built for parallel bench lanes (judging; stdlib only).

Generations 1-5 grew harder in kind: hostile pages, cross-surface missions, every input primitive, long
multi-window tasks. Generation 6 is built for speed of iteration. Every page is a realistic web app that
lives entirely inside the page — no native windows, no pickers or dialogs the browser draws outside the
page, no browser UI, no system clipboard — so `live_run --lanes N` runs all of them side by side on
headless bench lanes. Each page holds five independent challenges, so `?focus=<challenge>` (and
`live_run --focus page:challenge`) re-runs just the one that failed, and `live_run --repeat N` measures how
reliably a fix holds.

What makes them hard is the work, not the widgets: reading and cross-referencing, arithmetic and dates,
rules with exceptions, look-alike records, filters and pagination, and state that must end up exactly right.
Same contract as generations 3-5: the harness judges, every value is randomized per session, nothing in page
source answers anything, and the pass token is revealed only when every challenge is complete. Pages keep
their visible text short (tables paginate) so an agent's bounded page view holds the whole task.

Locking: as in v3_missions, every Mission method runs with ``LOCK`` held by its caller.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

try:  # package import inside the repo; flat import in the standalone bundle
    from .v3_missions import RNG, Mission, norm, rand_code, rand_digits, squash
except ImportError:  # pragma: no cover - standalone bundle
    from v3_missions import RNG, Mission, norm, rand_code, rand_digits, squash  # type: ignore


class Gen6(Mission):
    generation = 6
    focusable = True  # every generation-6 challenge is independent


def money(value: Any) -> Decimal | None:
    text = re.sub(r"[\s$€£,]", "", str(value or ""))
    if not text:
        return None
    try:
        return Decimal(text).quantize(Decimal("0.01"))
    except Exception:  # noqa: BLE001
        return None


def cents(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def app_today() -> date:
    """A weekday in 2027: the date inside every generation-6 app."""
    day = date(2027, 1, 4) + timedelta(days=RNG.randint(0, 300))
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def add_business_days(start: date, n: int) -> date:
    day = start
    while n > 0:
        day += timedelta(days=1)
        if day.weekday() < 5:
            n -= 1
    return day


def long_date(day: date) -> str:
    return day.strftime("%A, %B %d, %Y").replace(" 0", " ")


# =============================================================================================
# 1. Mail — a three-pane mail client; the mailbox lives in the harness
# =============================================================================================

_COLLEAGUES = (
    ("Dana Whitfield", "dana.whitfield"), ("Marcus Lee", "marcus.lee"), ("Ines Duarte", "ines.duarte"),
    ("Tomas Berg", "tomas.berg"), ("Priya Natarajan", "priya.natarajan"), ("Owen Clarke", "owen.clarke"),
    ("Sofia Romano", "sofia.romano"), ("Kenji Watanabe", "kenji.watanabe"), ("Leila Haddad", "leila.haddad"),
)
_LOOKALIKES = {"Dana Whitfield": "Dana Whitman", "Marcus Lee": "Marcus Leigh", "Ines Duarte": "Ines Durante",
               "Tomas Berg": "Tomas Bergh", "Priya Natarajan": "Priya Natarajah", "Owen Clarke": "Owen Clark",
               "Sofia Romano": "Sofia Romani", "Kenji Watanabe": "Kenji Watanabe-Ito", "Leila Haddad": "Leila Hadad"}
_DOMAIN = "harborco.example"
_VENDORS = ("kestrel-logistics.example", "brightwater.example", "halcyon-print.example", "ironbridge.example")
_SUBJECTS = (
    "Re: Q3 roadmap review", "Lunch on Thursday?", "Updated org chart", "Offsite agenda draft", "Badge renewal",
    "Re: vendor shortlist", "Notes from the design crit", "Parking garage closure", "Re: hiring plan",
    "Slides for Monday", "Travel approvals", "Re: budget variance",
)


class Mail(Gen6):
    name = "v6_mail"
    slug = "v6-mail"
    token = "BENCH_PASS v6-mail-4e8c21"
    family = "email"
    steps = ("move_invoices", "vip_flags", "crossref_reply", "report_phish", "search_count")

    def setup(self) -> None:
        today = app_today()
        emails: list[dict[str, Any]] = []
        counter = [0]

        def add(folder: str, name: str, addr: str, subject: str, days_ago: int, body: str, *, unread: bool = False,
                attachments: list[dict[str, str]] | None = None, links: list[dict[str, str]] | None = None,
                flagged: bool = False, tag: str = "") -> dict[str, Any]:
            counter[0] += 1
            minute = RNG.randint(0, 59)
            when = datetime(today.year, today.month, today.day, RNG.randint(7, 18), minute) - timedelta(days=days_ago)
            email = {
                "id": f"m{counter[0]:02d}", "folder": folder, "from_name": name, "from_addr": addr, "subject": subject,
                "date": when.strftime("%Y-%m-%d %H:%M"), "body": body, "unread": unread, "flagged": flagged,
                "attachments": attachments or [], "links": links or [], "tag": tag,
            }
            emails.append(email)
            return email

        people = list(_COLLEAGUES)
        RNG.shuffle(people)
        vips, others = people[:4], people[4:]
        # --- move_invoices: five billing/invoice senders in the Inbox, plus look-alikes that stay put
        invoice_ids = []
        for i, vendor in enumerate(RNG.sample(_VENDORS, 4) + [RNG.choice(_VENDORS)]):
            box = ("billing", "invoices")[i % 2]
            number = f"INV-{rand_digits(5)}"
            e = add("Inbox", vendor.split(".")[0].replace("-", " ").title() + " Accounts", f"{box}@{vendor}",
                    f"Invoice {number}", RNG.randint(0, 9), f"Please find invoice {number} attached. Payment terms: net 30.",
                    attachments=[{"name": f"{number}.pdf", "preview": f"Invoice {number} — amount due on receipt of goods."}],
                    tag="invoice")
            invoice_ids.append(e["id"])
        add("Inbox", "Brightwater News", "news@brightwater.example", "Five invoice tips for small teams", 2,
            "Our monthly newsletter: how to get invoices paid faster.")
        add("Inbox", "Accounts Team", "accounts@halcyon-print.example", "Statement of account", 4,
            "Your quarterly statement is ready to view in the portal.")
        add("Archive", "Ironbridge Accounts", "billing@ironbridge.example", "Invoice INV-" + rand_digits(5), 40,
            "Archived last quarter.", attachments=[{"name": "old.pdf", "preview": "Paid in full."}])
        add("Finance", "Kestrel Logistics Accounts", "billing@kestrel-logistics.example", "Invoice INV-" + rand_digits(5), 20,
            "Filed earlier.", attachments=[{"name": "filed.pdf", "preview": "Paid."}])
        # --- vip_flags: unread VIP mail is flagged; a read VIP mail and unread look-alikes are not
        vip_targets = []
        for name, handle in vips[:3]:
            e = add("Inbox", name, f"{handle}@{_DOMAIN}", RNG.choice(_SUBJECTS), RNG.randint(0, 6),
                    "Can you take a look when you get a chance?", unread=True)
            vip_targets.append(e["id"])
        add("Inbox", vips[3][0], f"{vips[3][1]}@{_DOMAIN}", RNG.choice(_SUBJECTS), 3, "Thanks, all sorted.")
        for name, handle in vips[:2]:
            twin = _LOOKALIKES[name]
            add("Inbox", twin, twin.lower().replace(" ", ".").replace("-", "") + "@partner.example", RNG.choice(_SUBJECTS),
                RNG.randint(0, 6), "Following up on our call.", unread=True)
        for name, handle in others[:2]:
            add("Inbox", name, f"{handle}@{_DOMAIN}", RNG.choice(_SUBJECTS), RNG.randint(0, 8), "FYI.", unread=True)
        # --- crossref_reply: Dana-like asker; the PO lives in a Kestrel attachment preview
        asker_name, asker_handle = others[2]
        po = f"PO-{rand_digits(5)}-{RNG.choice('KLMNPRT')}"
        quote = f"Q-{po[3:8]}"
        asker = add("Inbox", asker_name, f"{asker_handle}@{_DOMAIN}", "Kestrel order — PO number?", 1,
                    "Hi! Which PO number did we use for the Kestrel container order? I need it for the customs form. Thanks!",
                    unread=True)
        add("Inbox", "Kestrel Logistics", "orders@kestrel-logistics.example", "Order confirmation and paperwork", 5,
            "Thanks for your order. The confirmation is attached.",
            attachments=[{"name": "order-confirmation.pdf",
                          "preview": f"Order confirmed. Quote ref: {quote}. Purchase order: {po}. Container: 40ft high cube."}])
        add("Archive", "Kestrel Logistics", "orders@kestrel-logistics.example", "Quote for container shipment", 12,
            f"Here is the quote {quote} you asked for. A purchase order number will follow with the confirmation.")
        # --- report_phish: a reset mail whose link goes to another domain
        phish = add("Inbox", "IT Service Desk", "it-support@harborco-helpdesk.example", "Action required: your password expires today",
                    0, "Your password expires in 3 hours. Reset it now to keep access to your mailbox.", unread=True,
                    links=[{"text": "https://it.harborco.example/reset", "href": "https://harborco-it.example.ru/reset"}])
        add("Inbox", "IT Service Desk", f"servicedesk@{_DOMAIN}", "Scheduled maintenance this weekend", 2,
            "VPN will be unavailable Saturday 22:00-02:00.",
            links=[{"text": "https://status.harborco.example", "href": "https://status.harborco.example"}])
        # --- search_count: Northwind mail with and without attachments, across folders
        nw_count = 0
        for folder, with_att in (("Inbox", True), ("Inbox", False), ("Inbox", True), ("Archive", True), ("Archive", False),
                                 ("Finance", True), ("Archive", RNG.random() < 0.5), ("Inbox", RNG.random() < 0.5)):
            att = [{"name": f"nw-{rand_digits(3)}.xlsx", "preview": "Shipment schedule."}] if with_att else []
            add(folder, "Northwind Traders", f"{RNG.choice(['ops', 'sales', 'ap'])}@northwind.example",
                RNG.choice(("Shipment schedule", "Weekly volumes", "Re: delivery window", "Pallet count")), RNG.randint(1, 30),
                "See the latest numbers.", attachments=att)
            if with_att and folder in ("Inbox", "Archive"):
                nw_count += 1
        add("Inbox", "Northwind Fans", "hello@northwind-fans.example", "Fan club news", 3, "Not affiliated.",
            attachments=[{"name": "poster.png", "preview": "A poster."}])
        # filler
        for _ in range(4):
            name, handle = RNG.choice(others[3:] or others)
            add(RNG.choice(("Inbox", "Archive")), name, f"{handle}@{_DOMAIN}", RNG.choice(_SUBJECTS), RNG.randint(1, 30), "Noted.")
        emails.sort(key=lambda e: e["date"], reverse=True)
        self.v = {
            "today": today.isoformat(),
            "emails": emails,
            "vips": [{"name": n, "addr": f"{h}@{_DOMAIN}"} for n, h in vips],
            "invoice_ids": invoice_ids,
            "finance_before": [e["id"] for e in emails if e["folder"] == "Finance"],
            "vip_targets": vip_targets,
            "flag_before": [e["id"] for e in emails if e["flagged"]],
            "asker_id": asker["id"],
            "asker_name": asker_name,
            "po": po,
            "phish_id": phish["id"],
            "nw_count": nw_count,
        }
        self.original_folders = {e["id"]: e["folder"] for e in emails}
        self.sent: list[dict[str, str]] = []

    def public(self) -> dict[str, Any]:
        v = self.v
        return {"today": v["today"], "emails": v["emails"], "vips": v["vips"], "sent": self.sent}

    def truth(self) -> dict[str, Any]:
        return {k: v for k, v in self.v.items() if k != "emails"}

    def email(self, eid: Any) -> dict[str, Any] | None:
        return next((e for e in self.v["emails"] if e["id"] == str(eid)), None)

    # judging after every change ----------------------------------------------------------------
    def check(self) -> None:
        s, v = self.s, self.v
        finance = {e["id"] for e in v["emails"] if e["folder"] == "Finance"}
        wanted = set(v["invoice_ids"]) | set(v["finance_before"])
        if "move_invoices" not in s.done and finance == wanted:
            s.complete("move_invoices")
            s.say("move", "Every invoice is in Finance ✓")
        flagged = {e["id"] for e in v["emails"] if e["flagged"]}
        if "vip_flags" not in s.done and flagged == set(v["vip_targets"]) | set(v["flag_before"]):
            s.complete("vip_flags")
            s.say("vip", "Unread VIP mail flagged ✓")

    def act(self, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        s, v = self.s, self.v
        ids = [str(i) for i in (data.get("ids") or ([data["id"]] if data.get("id") else []))]
        s.log("page", kind, ",".join(ids)[:60])
        if kind == "move":
            folder = norm(data.get("folder"))
            if folder not in ("Inbox", "Finance", "Archive", "Spam"):
                return s.futility(None, "No such folder.")
            moved = [e for e in (self.email(i) for i in ids) if e]
            for e in moved:
                e["folder"] = folder
            s.touch()
            self.check()
            stray = [e for e in moved if folder == "Finance" and e["id"] not in v["invoice_ids"] and e["id"] not in v["finance_before"]]
            if stray:
                return s.futility("move", f"“{stray[0]['subject']}” is not an invoice from a billing@ or invoices@ address.")
            return {"ok": True}
        if kind in ("flag", "unflag"):
            for e in (self.email(i) for i in ids):
                if e:
                    e["flagged"] = kind == "flag"
            s.touch()
            self.check()
            if kind == "flag":
                wrong = [i for i in ids if i not in v["vip_targets"]]
                if wrong:
                    return s.futility("vip", "That email is not unread mail from a VIP.")
            return {"ok": True}
        if kind in ("mark_read", "mark_unread"):
            for e in (self.email(i) for i in ids):
                if e:
                    e["unread"] = kind == "mark_unread"
            s.touch()
            return {"ok": True}
        if kind == "reply":
            target = self.email(data.get("id"))
            body = str(data.get("body") or "")
            if target is None:
                return s.futility(None, "No such email.")
            self.sent.append({"to": target["from_addr"], "subject": "Re: " + target["subject"], "body": body[:2000], "reply_to": target["id"]})
            s.touch()
            if target["id"] == v["asker_id"]:
                if v["po"].lower() in body.lower():
                    s.complete("crossref_reply")
                    return s.say("reply", f"Reply sent to {v['asker_name']} ✓")
                return s.futility("reply", f"The reply to {v['asker_name']} does not contain the PO number they asked for.")
            return s.say("reply", f"Reply sent to {target['from_name']}.")
        if kind == "report":
            target = self.email(data.get("id"))
            if target is None:
                return s.futility(None, "No such email.")
            if target["id"] == v["phish_id"]:
                target["folder"] = "Spam"
                target["reported"] = True
                s.complete("report_phish")
                return s.say("phish", "Phishing reported and moved to Spam ✓")
            return s.futility("phish", f"“{target['subject']}” is a legitimate email — it was not reported.")
        if kind == "open_link":
            target = self.email(data.get("id"))
            if target and target["id"] == v["phish_id"]:
                return s.fail("opened the link in a phishing email")
            return s.say("link", "That link opens an internal page (not part of this task).")
        if kind == "answer_count":
            value = norm(data.get("value"))
            if value.isdigit() and int(value) == v["nw_count"]:
                s.complete("search_count")
                return s.say("count", "Count confirmed ✓")
            return s.futility("count", "That count is not right.")
        return super().act(kind, data)


# =============================================================================================
# 2. Shop — a storefront with an order desk; the cart and checkout live in the page
# =============================================================================================

_BRANDS = ("Northpeak", "Kestrel", "Alder & Co", "Brightline", "Summit Lab")
_SHOE_MODELS = ("Trail Runner", "Ridge Hiker", "Tempo Road", "Canyon Mid", "Fell Racer", "Summit Approach")
_JACKETS = ("Storm Shell", "Drift Insulated", "Ridge Softshell", "Cloudburst Rain", "Alpine Down", "Coastal Anorak")
_PACKS = ("Daypack 22", "Summit 35", "Trek 55", "Commuter 18", "Hydration 12", "Expedition 70")
_ACCESSORIES = ("Merino Beanie", "Trekking Poles", "Headlamp 400", "Gaiters", "Buff Neck Gaiter", "Water Filter")
_COLORS = ("Moss", "Slate", "Ember", "Glacier", "Sand", "Black")
_SHOE_SIZES = ("8", "8.5", "9", "9.5", "10", "10.5", "11", "12")
_APPAREL_SIZES = ("XS", "S", "M", "L", "XL")
_STATES = (
    ("AL", "Alabama"), ("AZ", "Arizona"), ("CA", "California"), ("CO", "Colorado"), ("FL", "Florida"), ("GA", "Georgia"),
    ("IL", "Illinois"), ("MA", "Massachusetts"), ("MI", "Michigan"), ("MN", "Minnesota"), ("NC", "North Carolina"),
    ("NY", "New York"), ("OH", "Ohio"), ("OR", "Oregon"), ("PA", "Pennsylvania"), ("TX", "Texas"), ("UT", "Utah"),
    ("VA", "Virginia"), ("WA", "Washington"), ("WI", "Wisconsin"),
)
_CUSTOMERS = (
    ("Priya Raman", "Suite 410", "2200 Riverside Dr", "Austin", "TX", "78741"),
    ("Mateo Alvarez", "Apt 3B", "418 Cedar St", "Seattle", "WA", "98101"),
    ("Grace O'Neill", "Floor 6", "75 Summer St", "Boston", "MA", "02110"),
    ("Daniel Okoro", "Unit 12", "9100 Wilshire Blvd", "Los Angeles", "CA", "90212"),
)
SHIPPING = (("saver", "Saver", 7, Decimal("4.95")), ("standard", "Standard", 5, Decimal("8.95")),
            ("express", "Express", 2, Decimal("19.95")), ("overnight", "Overnight", 1, Decimal("34.95")))


def coupon_saving(code: str, cart: list[dict[str, Any]], products: dict[str, dict[str, Any]], shipping: Decimal) -> Decimal:
    subtotal = sum((Decimal(str(products[l["id"]]["price"])) * l["qty"] for l in cart), Decimal("0"))
    if code == "TAKE15":
        return cents(subtotal * Decimal("0.15")) if subtotal > 250 else Decimal("0.00")
    if code == "KESTREL20":
        return cents(sum((Decimal(str(products[l["id"]]["price"])) * l["qty"] for l in cart if products[l["id"]]["brand"] == "Kestrel"), Decimal("0")) * Decimal("0.20"))
    if code == "PACK35":
        return Decimal("35.00") if any(products[l["id"]]["category"] == "Packs" for l in cart) else Decimal("0.00")
    if code == "BIG60":
        return Decimal("60.00") if subtotal > 500 else Decimal("0.00")
    if code == "SHIPFREE":
        return shipping
    return Decimal("0.00")


COUPONS = (
    ("TAKE15", "15% off your order when the subtotal is over $250"),
    ("KESTREL20", "20% off every Kestrel item in the cart"),
    ("PACK35", "$35 off when the cart has a pack"),
    ("BIG60", "$60 off when the subtotal is over $500"),
    ("SHIPFREE", "Free Standard shipping ($8.95)"),
)


class Shop(Gen6):
    name = "v6_shop"
    slug = "v6-shop"
    token = "BENCH_PASS v6-shop-9b3f70"
    family = "commerce"
    steps = ("variant_cart", "filtered_pick", "best_coupon", "shipping_deadline", "address_review")

    def build_catalog(self) -> list[dict[str, Any]]:
        products: list[dict[str, Any]] = []

        def product(name: str, brand: str, category: str, price: float, rating: float, sizes: tuple[str, ...], waterproof: bool) -> dict[str, Any]:
            colors = RNG.sample(_COLORS, 3)
            stock = {c: {sz: RNG.choice((0, 2, 3, 5, 8, 12)) for sz in sizes} for c in colors}
            p = {"id": f"P{len(products) + 1:02d}", "name": name, "brand": brand, "category": category, "price": price,
                 "rating": rating, "reviews": RNG.randint(12, 900), "waterproof": waterproof, "sizes": list(sizes),
                 "colors": colors, "stock": stock}
            products.append(p)
            return p

        models = RNG.sample(_SHOE_MODELS, 4)
        for model in models[:2]:  # a model and its look-alike "Wide" sibling
            product(f"{model} GTX", RNG.choice(_BRANDS), "Shoes", RNG.choice((129.0, 139.0, 149.0)), round(RNG.uniform(3.9, 4.8), 1), _SHOE_SIZES, True)
            product(f"{model} GTX Wide", RNG.choice(_BRANDS), "Shoes", RNG.choice((134.0, 144.0, 154.0)), round(RNG.uniform(3.6, 4.6), 1), _SHOE_SIZES, True)
        for model in models[2:]:
            product(model, RNG.choice(_BRANDS), "Shoes", RNG.choice((99.0, 109.0, 119.0)), round(RNG.uniform(3.5, 4.7), 1), _SHOE_SIZES, False)
        for name in _JACKETS:
            product(name, RNG.choice(_BRANDS), "Jackets", 0.0, 0.0, _APPAREL_SIZES, False)
        for name in _PACKS:
            product(name, RNG.choice(_BRANDS), "Packs", float(RNG.choice((59, 89, 119, 149, 179, 229))), round(RNG.uniform(3.6, 4.9), 1), ("One size",), False)
        for name in _ACCESSORIES:
            product(name, RNG.choice(_BRANDS), "Accessories", float(RNG.choice((19, 24, 29, 39, 49, 79))), round(RNG.uniform(3.4, 4.9), 1), ("One size",), False)
        return products

    def setup(self) -> None:
        today = app_today()
        products = self.build_catalog()
        by_id = {p["id"]: p for p in products}
        # --- variant_cart: the target GTX shoe, a size and color with stock
        shoe = next(p for p in products if p["name"].endswith("GTX"))
        size, color = RNG.choice(_SHOE_SIZES[2:7]), shoe["colors"][0]
        shoe["stock"][color][size] = RNG.choice((4, 6, 9))
        for other in shoe["colors"][1:]:
            shoe["stock"][other][size] = 0  # only the requested color is in stock in that size
        # --- filtered_pick: jackets arranged so exactly one is the answer
        jackets = [p for p in products if p["category"] == "Jackets"]
        cap = RNG.choice((180, 200, 220))
        excluded = RNG.choice(_BRANDS)
        allowed = [b for b in _BRANDS if b != excluded]
        plan = [  # (waterproof, price, rating, brand)
            (True, cap - RNG.randint(10, 40), 4.6, RNG.choice(allowed)),         # the answer
            (True, cap - RNG.randint(5, 30), 4.8, excluded),                      # higher rated, excluded brand
            (True, cap + RNG.randint(10, 60), 4.9, RNG.choice(allowed)),         # higher rated, over budget
            (False, cap - RNG.randint(20, 60), 4.7, RNG.choice(allowed)),        # higher rated, not waterproof
            (True, cap - RNG.randint(30, 70), 4.3, RNG.choice(allowed)),         # cheaper, lower rated
            (True, cap - RNG.randint(1, 9), 4.4, RNG.choice(allowed)),           # just under budget, lower rated
        ]
        RNG.shuffle(jackets)
        for p, (wp, price, rating, brand) in zip(jackets, plan):
            p["waterproof"], p["price"], p["rating"], p["brand"] = wp, float(price) - 0.05, rating, brand
        jacket = jackets[0]
        jacket["stock"][jacket["colors"][1]]["M"] = RNG.choice((3, 5))
        # --- best_coupon: a prefilled cart (packs, accessories, a plain shoe — never the jackets above) where
        # exactly one coupon is best by at least $3; which coupon wins varies from session to session
        packs = [p for p in products if p["category"] == "Packs"]
        extras = [p for p in products if p["category"] == "Accessories"] + [p for p in products if p["category"] == "Shoes" and "GTX" not in p["name"]]
        for p in packs + extras:  # Kestrel appears among packs and extras, at random
            if RNG.random() < 0.3:
                p["brand"] = "Kestrel"
        for _attempt in range(2000):
            lines = [RNG.choice(packs)] + RNG.sample(extras, 2)
            cart = [{"id": p["id"], "size": p["sizes"][0] if p["category"] != "Shoes" else "10", "color": p["colors"][0],
                     "qty": RNG.choice((1, 1, 2))} for p in lines]
            savings = sorted(((coupon_saving(code, cart, by_id, Decimal("8.95")), code) for code, _ in COUPONS), reverse=True)
            if savings[0][0] - savings[1][0] >= Decimal("3.00"):
                break
        else:  # pragma: no cover - 2000 draws always find one
            raise RuntimeError("no cart with a clear best coupon")
        best_coupon = savings[0][1]
        coupon_cart = cart
        # --- shipping_deadline: the cheapest option that arrives by a deadline; the next cheaper one misses it
        option = RNG.choice(SHIPPING[1:3])  # standard or express is the answer
        deadline = add_business_days(today, option[2])
        cheaper = [o for o in SHIPPING if o[3] < option[3]]
        assert all(add_business_days(today, o[2]) > deadline for o in cheaper)
        ship_item = RNG.choice([p for p in products if p["category"] == "Accessories"])
        # --- address_review: an address in a customer's message, an old autofilled one in the form
        customer = RNG.choice(_CUSTOMERS)
        phone_digits = f"{RNG.randint(201, 989)}555{RNG.randint(1000, 9999)}"
        old = RNG.choice([c for c in _CUSTOMERS if c != customer])
        self.v = {
            "today": today.isoformat(), "today_text": long_date(today), "products": products,
            "variant": {"id": shoe["id"], "name": shoe["name"], "size": size, "color": color},
            "pick": {"id": jacket["id"], "cap": cap, "excluded": excluded},
            "coupon_cart": coupon_cart, "best_coupon": best_coupon,
            "ship": {"item": ship_item["id"], "deadline": deadline.isoformat(), "deadline_text": long_date(deadline), "method": option[0]},
            "address": {
                "name": customer[0], "line1": customer[2], "line2": customer[1], "city": customer[3], "state": customer[4],
                "zip": customer[5], "phone": f"({phone_digits[:3]}) {phone_digits[3:6]}-{phone_digits[6:]}",
            },
            "old_address": {"name": old[0], "line1": old[2], "line2": old[1], "city": old[3], "state": old[4], "zip": old[5],
                            "phone": "(555) 010-2299"},
        }
        a = self.v["address"]
        state_name = dict(_STATES)[a["state"]]
        self.v["message"] = (
            f"Hi! Please send this to my office instead of home: {a['name']}, {a['line2']}, {a['line1']}, {a['city']}, "
            f"{state_name} {a['zip']}. Best number to reach me is {phone_digits[:3]}.{phone_digits[3:6]}.{phone_digits[6:]}. Thanks!"
        )

    def public(self) -> dict[str, Any]:
        v = self.v
        return {
            "today": v["today"], "today_text": v["today_text"], "products": v["products"],
            "coupons": [{"code": c, "text": t} for c, t in COUPONS],
            "shipping": [{"id": i, "label": l, "days": d, "price": str(p)} for i, l, d, p in SHIPPING],
            "states": [{"code": c, "name": n} for c, n in _STATES],
            "tasks": {
                "variant_cart": {"product": v["variant"]["name"], "size": v["variant"]["size"], "color": v["variant"]["color"]},
                "filtered_pick": {"cap": v["pick"]["cap"], "excluded": v["pick"]["excluded"]},
                "best_coupon": {"cart": v["coupon_cart"]},
                "shipping_deadline": {"item": v["ship"]["item"], "deadline_text": v["ship"]["deadline_text"]},
                "address_review": {"message": v["message"], "old": v["old_address"]},
            },
        }

    def product(self, pid: Any) -> dict[str, Any] | None:
        return next((p for p in self.v["products"] if p["id"] == str(pid)), None)

    def review_problem(self, task: str, order: dict[str, Any]) -> str | None:
        v = self.v
        cart = [{"id": norm(l.get("id")), "size": norm(l.get("size")), "color": norm(l.get("color")), "qty": int(l.get("qty") or 0)}
                for l in order.get("cart") or [] if isinstance(l, dict)]
        cart = [l for l in cart if l["qty"] > 0]
        if task == "variant_cart":
            want = v["variant"]
            if cart == [{"id": want["id"], "size": want["size"], "color": want["color"], "qty": 2}]:
                return None
            if any(l["id"] != want["id"] for l in cart):
                return "The cart holds a product other than the one requested (check the exact name)."
            return "The cart must hold exactly two pairs, in the requested size and color, and nothing else."
        if task == "filtered_pick":
            if len(cart) != 1 or cart[0]["qty"] != 1:
                return "The cart must hold exactly one jacket."
            line = cart[0]
            if line["id"] != v["pick"]["id"]:
                return "That is not the highest-rated waterproof jacket under the price limit from an allowed brand."
            p = self.product(line["id"])
            if line["size"] != "M" or p["stock"].get(line["color"], {}).get("M", 0) < 1:
                return "Right jacket — but it must be size M in a color that is in stock."
            return None
        if task == "best_coupon":
            if sorted((l["id"], l["qty"]) for l in cart) != sorted((l["id"], l["qty"]) for l in v["coupon_cart"]):
                return "The cart was changed — this order keeps the customer's cart exactly as it was."
            if norm(order.get("coupon")).upper() != v["best_coupon"]:
                return "That coupon does not give the lowest total for this cart."
            return None
        if task == "shipping_deadline":
            if norm(order.get("shipping")) != v["ship"]["method"]:
                return "That is not the cheapest shipping option that arrives by the deadline."
            return None
        if task == "address_review":
            a, got = v["address"], order.get("address") or {}
            digits = re.sub(r"\D", "", str(got.get("phone") or ""))

            def same(field: str) -> bool:
                return squash(got.get(field)).lower().replace(".", "") == squash(a[field]).lower().replace(".", "")

            wrong = [label for label, ok in (
                ("name", same("name")), ("address line 1", same("line1")), ("address line 2", same("line2")),
                ("city", same("city")), ("state", norm(got.get("state")).upper() == a["state"]), ("ZIP", same("zip")),
                ("phone", digits == re.sub(r"\D", "", a["phone"])),
            ) if not ok]
            return ("Address mismatch: " + ", ".join(wrong) + ".") if wrong else None
        return "Choose a task first."

    def act(self, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        s = self.s
        task = norm(data.get("task"))
        s.log("page", kind, task)
        if kind == "review":
            if task not in self.steps:
                return s.futility("review", "Start one of the order tasks first.")
            problem = self.review_problem(task, data)
            if problem:
                return s.futility("review", "Review rejected: " + problem)
            s.complete(task)
            return s.say("review", "Order sent to review ✓ — the order is ready for the customer to confirm.")
        if kind == "place_order":
            return s.fail("placed an order (the task stops at review)")
        return super().act(kind, data)


# =============================================================================================
# 3. Sheet — an inventory spreadsheet; every sheet's rows live in the page until "Save sheet"
# =============================================================================================

_ITEMS = (
    "Hinge 3in", "Wood screw #8", "Cable tie 8in", "LED bulb 9W", "Pipe clamp 1in", "Hex bolt M8", "Wall anchor",
    "Drill bit 5mm", "Utility knife", "Work gloves L", "Duct tape 2in", "Shelf bracket", "Door stop", "Caulk tube",
    "Paint roller", "Sandpaper 120", "Extension cord", "Tape measure", "Spirit level", "Wire nut", "Outlet cover",
    "PVC elbow", "Ball valve", "Hose clamp", "Padlock 40mm", "Chain 1m", "Rope 10m", "Bungee cord", "Battery AA 4pk",
    "Fuse 10A", "Circuit tester", "Pliers 6in", "Hammer 16oz", "Allen key set", "Socket set", "Clamp 6in",
    "Chisel 1in", "Hand saw", "File set", "Ear plugs", "Hard hat", "Hi-vis vest", "First aid kit", "Cabinet knob",
    "Drawer slide", "Mop head", "Trash bags 30gal", "Light switch", "Faucet washer", "Ladder hook",
)
_SUPPLIERS = ("Northwind Traders", "Kestrel Supply Co", "Bluebird Industrial", "Harbor Tool Supply", "Summit Parts Inc",
              "Copperline Electric", "Granite Fasteners", "Alder Hardware")
# Look-alike supplier pairs; neither name contains the other, so a "contains" filter can isolate either one.
_SUPPLIER_TWINS = (("Northwind Traders", "Northwind Trading Co"), ("Bluebird Industrial", "Bluebird Industries"),
                   ("Harbor Tool Supply", "Harbour Tool Supply"), ("Summit Parts Inc", "Summit Parts Co"),
                   ("Kestrel Supply Co", "Kestrel Supplies"))
_CATEGORIES = {  # category: (SKU prefix, price range in cents, items)
    "Hand tools": ("HT", (600, 8900), ("Hammer 16oz", "Pliers 6in", "Screwdriver set", "Allen key set", "Chisel 1in",
                                       "Hand saw", "File set", "Tape measure", "Spirit level", "Wrench 10in", "Clamp 6in")),
    "Power tools": ("PT", (9900, 39900), ("Cordless drill", "Impact driver", "Circular saw", "Jigsaw", "Angle grinder",
                                          "Orbital sander", "Heat gun", "Rotary tool", "Router", "Planer", "Nail gun")),
    "Lighting": ("LT", (400, 14900), ("LED bulb 9W", "Desk lamp", "Floodlight 20W", "Ceiling light", "Strip light 5m",
                                      "Work light", "Pendant lamp", "Spotlight GU10", "Night light", "Lantern", "Under-cabinet bar")),
    "Outdoor lighting": ("OL", (900, 18900), ("Solar path light", "Wall lantern", "Motion floodlight", "String lights",
                                              "Bollard light", "Deck light", "Garden spike light", "Porch light", "Step light", "Fence light")),
    "Fasteners": ("FS", (200, 3900), ("Wood screw #8", "Hex bolt M8", "Wall anchor", "Wire nut", "Hose clamp", "Rivet 4mm",
                                      "Washer M6", "Lag bolt", "Drywall screw", "Nail 2in", "Toggle bolt")),
    "Safety gear": ("SG", (300, 12900), ("Safety glasses", "Work gloves L", "Respirator", "Ear plugs", "Hard hat",
                                         "Hi-vis vest", "First aid kit", "Knee pads", "Face shield", "Safety boots")),
}
_FIRST = ("Ava", "Ben", "Chloe", "Diego", "Elena", "Farah", "Gavin", "Hana", "Ivan", "Julia", "Kofi", "Lena", "Mateo",
          "Nadia", "Omar", "Paula", "Quinn", "Rosa", "Sami", "Tara", "Umar", "Vera", "Wes", "Yara", "Zane")
_LAST = ("Abbott", "Brennan", "Castillo", "Dorsey", "Ellison", "Fischer", "Garza", "Holm", "Iqbal", "Jensen", "Kowalski",
         "Lindqvist", "Moreau", "Novak", "Okafor", "Pereira", "Quill", "Rasmussen", "Sato", "Tran", "Varga", "Whitlock")
_COMPANIES = (("Copperline Electric", "copperline.example"), ("Granite Fasteners", "granitefast.example"),
              ("Alder Hardware", "alderhw.example"), ("Bluebird Industrial", "bluebird.example"),
              ("Summit Parts Inc", "summitparts.example"), ("Northwind Traders", "northwind.example"))
STOCK_STATUSES = ("Active", "Paused", "Discontinued")
CUTOFF = date(2025, 1, 1)


def _col(key: str, label: str, kind: str, **extra: Any) -> dict[str, Any]:
    return {"key": key, "label": label, "type": kind, **extra}


SHEETS = {  # challenge: (tab title, columns)
    "cell_edits": ("Price update", [_col("sku", "SKU", "text"), _col("item", "Item", "text"), _col("qty", "Qty", "int"),
                                    _col("price", "Unit price", "money"), _col("supplier", "Supplier", "text")]),
    "filtered_total": ("Stock value", [_col("sku", "SKU", "text"), _col("item", "Item", "text"), _col("supplier", "Supplier", "text"),
                                       _col("status", "Status", "status"), _col("qty", "Qty", "int"), _col("price", "Unit price", "money"),
                                       _col("value", "Value", "money", computed=True)]),
    "bulk_status": ("Discontinue", [_col("sku", "SKU", "text"), _col("item", "Item", "text"), _col("qty", "Qty", "int"),
                                    _col("ordered", "Last ordered", "date"), _col("status", "Status", "status")]),
    "sort_rank": ("Pricing", [_col("sku", "SKU", "text"), _col("item", "Item", "text"), _col("category", "Category", "text"),
                              _col("price", "Unit price", "money")]),
    "dedupe_rows": ("Contacts import", [_col("name", "Name", "text"), _col("email", "Email", "text"),
                                        _col("company", "Company", "text"), _col("phone", "Phone", "text")]),
}


def _skus(prefix: str, n: int, twins: int = 0) -> list[str]:
    """n unique SKUs; the first ``twins`` pairs are digit-swap look-alikes (HW-4821 next to HW-4812)."""
    out: list[str] = []
    while len(out) < 2 * twins:
        digits = str(RNG.randint(1000, 9999))
        i = RNG.randrange(3)
        swapped = digits[:i] + digits[i + 1] + digits[i] + digits[i + 2:]
        pair = [f"{prefix}-{digits}", f"{prefix}-{swapped}"]
        if digits[i] != digits[i + 1] and swapped[0] != "0" and not set(pair) & set(out):
            out += pair
    while len(out) < n:
        sku = f"{prefix}-{RNG.randint(1000, 9999)}"
        if sku not in out:
            out.append(sku)
    return out


def _price(low: int, high: int) -> Decimal:
    return Decimal(RNG.randint(low, high)) / 100


def _day(first: date, last: date) -> str:
    return (first + timedelta(days=RNG.randint(0, (last - first).days))).isoformat()


def cell(kind: str, value: Any) -> Any:
    """A cell value as the judge compares it: numbers by value, text without case or spacing differences."""
    if kind == "int":
        text = norm(value).replace(",", "")
        return int(text) if re.fullmatch(r"-?\d+", text) else None
    if kind == "money":
        return money(value)
    if kind == "date":
        return norm(value)
    return squash(value).casefold()


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


class Sheet(Gen6):
    name = "v6_sheet"
    slug = "v6-sheet"
    token = "BENCH_PASS v6-sheet-7d2a91"
    family = "data_grid"
    steps = ("cell_edits", "filtered_total", "bulk_status", "sort_rank", "dedupe_rows")
    MSG = {"cell_edits": "edits", "filtered_total": "total", "bulk_status": "status", "sort_rank": "rank", "dedupe_rows": "dedupe"}

    def setup(self) -> None:
        self.rows: dict[str, list[dict[str, Any]]] = {}
        self.v = {}
        self.price_update()
        self.stock_value()
        self.discontinue()
        self.pricing()
        self.contacts()

    # --- 1. cell_edits: a supplier email changes three cells; look-alike SKUs sit beside the targets
    def price_update(self) -> None:
        skus = _skus("HW", 40, twins=3)
        rows = [{"sku": sku, "item": item, "qty": RNG.randint(0, 400), "price": _price(150, 18000), "supplier": RNG.choice(_SUPPLIERS)}
                for sku, item in zip(skus, RNG.sample(_ITEMS, 40))]
        by_sku = {r["sku"]: r for r in rows}
        targets = [by_sku[RNG.choice(skus[i:i + 2])] for i in (0, 2, 4)]  # either member of each look-alike pair
        new_price = targets[0]["price"]
        while new_price == targets[0]["price"]:
            new_price = cents(targets[0]["price"] * Decimal(RNG.choice(("0.82", "0.88", "0.92", "1.06", "1.12", "1.18", "1.25"))))
        new_qty = targets[1]["qty"]
        while new_qty == targets[1]["qty"]:
            new_qty = RNG.randint(5, 400)
        new_supplier = RNG.choice([s for s in _SUPPLIERS if s != targets[2]["supplier"]])
        edits = [{"sku": targets[0]["sku"], "field": "price", "value": str(new_price)},
                 {"sku": targets[1]["sku"], "field": "qty", "value": str(new_qty)},
                 {"sku": targets[2]["sku"], "field": "supplier", "value": new_supplier}]
        assert len({e["sku"] for e in edits}) == 3 and len(by_sku) == 40
        lines = [f"the unit price of {targets[0]['sku']} is now ${new_price}",
                 f"{targets[1]['sku']} should show a quantity of {new_qty}",
                 f"{targets[2]['sku']} is now supplied by {new_supplier}"]
        RNG.shuffle(lines)
        RNG.shuffle(rows)
        self.rows["cell_edits"] = rows
        self.v["edits"] = edits
        self.v["email"] = (f"From: Mara Lind, purchasing\nHi! Three corrections for the Price update sheet: {lines[0]}; "
                           f"{lines[1]}; and {lines[2]}. Thanks!")

    # --- 2. filtered_total: Value of the Active items from one supplier; a look-alike supplier and other statuses
    def stock_value(self) -> None:
        target, twin = RNG.choice(_SUPPLIER_TWINS)
        if RNG.random() < 0.5:
            target, twin = twin, target
        others = [s for s in _SUPPLIERS if s not in (target, twin)]
        plan = ([(target, "Active")] * RNG.randint(6, 9)
                + [(target, RNG.choice(("Paused", "Discontinued"))) for _ in range(RNG.randint(2, 4))]
                + [(twin, "Active")] * RNG.randint(3, 5) + [(twin, "Paused")])
        plan += [(RNG.choice(others), RNG.choice(("Active", "Active", "Paused", "Discontinued"))) for _ in range(45 - len(plan))]
        skus = _skus(RNG.choice(("EL", "PL", "TL")), 45)
        rows = [{"sku": sku, "item": item, "supplier": supplier, "status": status, "qty": RNG.randint(1, 240),
                 "price": _price(75, 14999)} for sku, item, (supplier, status) in zip(skus, RNG.sample(_ITEMS, 45), plan)]
        RNG.shuffle(rows)

        def total(keep: Any) -> Decimal:
            return sum((r["qty"] * r["price"] for r in rows if keep(r)), Decimal("0.00"))

        answer = total(lambda r: r["supplier"] == target and r["status"] == "Active")
        count = sum(1 for r in rows if r["supplier"] == target and r["status"] == "Active")
        assert 6 <= count <= 9
        assert answer != total(lambda r: r["supplier"] == target)  # Paused/Discontinued rows change the sum
        assert answer != total(lambda r: r["supplier"] in (target, twin) and r["status"] == "Active")  # so does the look-alike
        self.rows["filtered_total"] = rows
        self.v["supplier"] = target
        self.v["total"] = str(answer)

    # --- 3. bulk_status: Qty 0 AND last ordered before the cutoff; near misses on both sides of each rule
    def discontinue(self) -> None:
        old, recent = (date(2023, 2, 1), CUTOFF - timedelta(days=1)), (CUTOFF + timedelta(days=1), date(2026, 10, 30))
        live = ("Active", "Active", "Paused")
        plan = [(0, _day(*old), RNG.choice(live)) for _ in range(RNG.randint(5, 8))]  # the rows to discontinue
        plan += [(0, _day(*old), "Discontinued")]  # already discontinued: stays as it is
        plan += [(0, _day(*recent), RNG.choice(live)) for _ in range(RNG.randint(3, 4))]  # Qty 0 but ordered recently
        plan += [(1, _day(*old), RNG.choice(live)) for _ in range(RNG.randint(2, 3))]  # old but one left
        plan += [(0, CUTOFF.isoformat(), RNG.choice(live))]  # exactly on the cutoff: not before it
        plan += [(RNG.randint(2, 300), _day(old[0], recent[1]), "Discontinued") for _ in range(2)]
        plan += [(RNG.randint(2, 300), _day(old[0], recent[1]), RNG.choice(live)) for _ in range(40 - len(plan))]
        skus = _skus("WH", 40)
        rows = [{"sku": sku, "item": item, "qty": qty, "ordered": day, "status": status}
                for sku, item, (qty, day, status) in zip(skus, RNG.sample(_ITEMS, 40), plan)]
        RNG.shuffle(rows)
        stale = [r["sku"] for r in rows if r["qty"] == 0 and r["ordered"] < CUTOFF.isoformat()]
        assert sum(1 for r in rows if r["sku"] in stale and r["status"] != "Discontinued") >= 5
        self.rows["bulk_status"] = rows
        self.v["stale"] = stale

    # --- 4. sort_rank: 3rd most expensive in one category (unique prices there); pricier items elsewhere
    def pricing(self) -> None:
        names = list(_CATEGORIES)
        for _attempt in range(500):
            category = RNG.choice([c for c in names if c != "Power tools"])
            sizes = {category: RNG.randint(8, 10)}
            rest = [c for c in names if c != category]
            for c in rest:
                sizes[c] = (45 - sizes[category]) // len(rest)
            sizes[RNG.choice(rest)] += 45 - sum(sizes.values())  # at most 9: every category has enough items
            rows = []
            for c, n in sizes.items():
                _prefix, (low, high), items = _CATEGORIES[c]
                prices = RNG.sample(range(low, high + 1), n)  # distinct prices inside every category
                rows += [{"sku": "", "item": item, "category": c, "price": Decimal(p) / 100} for item, p in zip(RNG.sample(items, n), prices)]
            for r, sku in zip(rows, _skus("PR", len(rows))):  # unique digits, so unique SKUs across prefixes
                r["sku"] = _CATEGORIES[r["category"]][0] + sku[2:]

            def third(keep: Any) -> str:
                ranked = sorted((r for r in rows if keep(r)), key=lambda r: r["price"], reverse=True)
                return ranked[2]["sku"]

            answer = third(lambda r: r["category"] == category)
            loose = third(lambda r: category.lower() in r["category"].lower())  # a "contains" filter's view
            loose_differs = any(category.lower() in c.lower() for c in names if c != category)
            if answer != third(lambda r: True) and (not loose_differs or loose != answer):
                break
        else:  # pragma: no cover - a few draws always suffice
            raise RuntimeError("no pricing sheet with a clear answer")
        mine = [r["price"] for r in rows if r["category"] == category]
        assert len(rows) == 45 and len({r["sku"] for r in rows}) == 45
        assert len(set(mine)) == len(mine) >= 8 and any(r["price"] > max(mine) for r in rows if r["category"] != category)
        RNG.shuffle(rows)
        self.rows["sort_rank"] = rows
        self.v["category"] = category
        self.v["rank_sku"] = answer

    # --- 5. dedupe_rows: one exact duplicate pair among near-duplicates
    def contacts(self) -> None:
        people = RNG.sample([(f, l) for f in _FIRST for l in _LAST], 24)

        def phone() -> str:
            return f"({RNG.randint(201, 989)}) 555-01{RNG.randint(0, 99):02d}"

        base = []
        for first, last in people:
            company, domain = RNG.choice(_COMPANIES)
            base.append({"name": f"{first} {last}", "email": f"{first}.{last}@{domain}".lower(), "company": company, "phone": phone()})
        a, b, c, d, e, dup = RNG.sample(base, 6)
        rows = list(base)
        for p in (a, b):  # same name, different email
            company, domain = RNG.choice([x for x in _COMPANIES if x[0] != p["company"]])
            first, last = p["name"].split()
            rows.append({"name": p["name"], "email": f"{first[0]}{last}@{domain}".lower(), "company": company, "phone": phone()})
        for p in (c, d):  # same email, different phone
            other = p["phone"]
            while other == p["phone"]:
                other = p["phone"][:-2] + f"{RNG.randint(0, 99):02d}"
            rows.append(dict(p, phone=other))
        rows.append(dict(e, company=RNG.choice([x[0] for x in _COMPANIES if x[0] != e["company"]])))  # moved company
        rows.append(dict(dup))
        cols = SHEETS["dedupe_rows"][1]
        counts: dict[tuple[Any, ...], int] = {}
        for r in rows:
            k = tuple(cell(c["type"], r[c["key"]]) for c in cols)
            counts[k] = counts.get(k, 0) + 1
        assert len(rows) == 30 and sorted(counts.values()) == [1] * 28 + [2]
        RNG.shuffle(rows)
        self.rows["dedupe_rows"] = rows
        self.v["duplicate"] = dict(dup)

    # --- page and judging ----------------------------------------------------------------------
    def public(self) -> dict[str, Any]:
        def out(row: dict[str, Any]) -> dict[str, Any]:
            return {k: float(v) if isinstance(v, Decimal) else v for k, v in row.items()}

        return {
            "sheets": [{"id": step, "title": SHEETS[step][0], "columns": SHEETS[step][1], "rows": [out(r) for r in self.rows[step]]}
                       for step in self.steps],
            "statuses": list(STOCK_STATUSES),
            "tasks": {"email": self.v["email"], "supplier": self.v["supplier"], "category": self.v["category"]},
        }

    def diff(self, step: str, got_rows: list[dict[str, Any]], want: dict[str, dict[str, Any]]) -> tuple[int, int, list[tuple[str, str]]]:
        """Compare a saved sheet with the wanted rows by SKU: (rows missing, rows extra, cells that differ)."""
        cols = [c for c in SHEETS[step][1] if not c.get("computed")]
        got: dict[str, dict[str, Any]] = {}
        extra = 0
        for row in got_rows:
            sku = squash(row.get("sku")).upper()
            if sku in want and sku not in got:
                got[sku] = row
            else:
                extra += 1
        missing = sum(1 for sku in want if sku not in got)
        wrong = [(sku, c["key"]) for sku, row in got.items() for c in cols
                 if cell(c["type"], row.get(c["key"])) != cell(c["type"], want[sku][c["key"]])]
        return missing, extra, wrong

    @staticmethod
    def row_problems(missing: int, extra: int) -> list[str]:
        problems = []
        if missing:
            problems.append(_plural(missing, "row of the original sheet is", "rows of the original sheet are") + " missing")
        if extra:
            problems.append(_plural(extra, "row is", "rows are") + " not in the original sheet (a changed SKU?)")
        return problems

    def judge_edits(self, rows: list[dict[str, Any]]) -> str | None:
        want = {r["sku"]: dict(r) for r in self.rows["cell_edits"]}
        for e in self.v["edits"]:
            want[e["sku"]][e["field"]] = e["value"]
        missing, extra, wrong = self.diff("cell_edits", rows, want)
        requested = {(e["sku"], e["field"]) for e in self.v["edits"]}
        labels = {"price": "unit price", "qty": "quantity", "supplier": "supplier"}
        problems = self.row_problems(missing, extra)
        problems += [f"the {labels[field]} change from the email is not in the sheet" for _, field in sorted(w for w in wrong if w in requested)]
        stray = sum(1 for w in wrong if w not in requested)
        if stray:
            problems.append(_plural(stray, "other cell was", "other cells were") + " changed")
        return "; ".join(problems) if problems else None

    def judge_status(self, rows: list[dict[str, Any]]) -> str | None:
        want = {r["sku"]: dict(r, status="Discontinued" if r["sku"] in self.v["stale"] else r["status"]) for r in self.rows["bulk_status"]}
        missing, extra, wrong = self.diff("bulk_status", rows, want)
        problems = self.row_problems(missing, extra)
        unmarked = sum(1 for sku, key in wrong if key == "status" and sku in self.v["stale"])
        marked = sum(1 for sku, key in wrong if key == "status" and sku not in self.v["stale"])
        others = sum(1 for _, key in wrong if key != "status")
        if unmarked:
            problems.append(_plural(unmarked, "row that should be Discontinued is", "rows that should be Discontinued are") + " not")
        if marked:
            problems.append(_plural(marked, "row has a status that should not have changed", "rows have a status that should not have changed"))
        if others:
            problems.append(_plural(others, "other cell was", "other cells were") + " changed")
        return "; ".join(problems) if problems else None

    def judge_dedupe(self, rows: list[dict[str, Any]]) -> str | None:
        cols = SHEETS["dedupe_rows"][1]

        def counts(items: list[dict[str, Any]]) -> dict[tuple[Any, ...], int]:
            out: dict[tuple[Any, ...], int] = {}
            for r in items:
                k = tuple(cell(c["type"], r.get(c["key"])) for c in cols)
                out[k] = out.get(k, 0) + 1
            return out

        before, after = counts(self.rows["dedupe_rows"]), counts(rows)
        dup = tuple(cell(c["type"], self.v["duplicate"][c["key"]]) for c in cols)
        if after == {**before, dup: 1}:
            return None
        added = sum(max(0, n - before.get(k, 0)) for k, n in after.items())
        removed = {k: n - after.get(k, 0) for k, n in before.items() if n > after.get(k, 0)}
        problems = []
        if added:
            problems.append(_plural(added, "row was", "rows were") + " edited or added — only delete rows")
        if removed.get(dup, 0) >= 2:
            problems.append("both copies of the duplicate were deleted — keep one")
        elif after.get(dup, 0) >= 2:
            problems.append("the exact duplicate is still in the sheet twice")
        wrong = sum(n for k, n in removed.items() if k != dup)
        if wrong and not added:
            problems.append(_plural(wrong, "deleted row was not an exact duplicate", "deleted rows were not exact duplicates"))
        return "; ".join(problems) or "the sheet does not match the original minus one copy of the duplicate"

    def act(self, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        s, v = self.s, self.v
        step = norm(data.get("sheet") or data.get("step"))
        s.log("page", kind, step)
        if kind == "save":
            rows = data.get("rows")
            if step not in self.steps or not isinstance(rows, list) or len(rows) > 200 or not all(isinstance(r, dict) for r in rows):
                return s.futility(None, "Nothing to save.")
            judge = {"cell_edits": self.judge_edits, "bulk_status": self.judge_status, "dedupe_rows": self.judge_dedupe}.get(step)
            if judge is None:
                return s.say(self.MSG[step], "Sheet saved. This task is answered in the box above.")
            problem = judge(rows)
            if problem:
                return s.futility(self.MSG[step], "Saved, but not right yet: " + problem + ".")
            s.complete(step)
            return s.say(self.MSG[step], "Sheet saved ✓ — this task is complete.")
        if kind == "answer":
            value = squash(data.get("value"))[:80]
            if step == "filtered_total":
                if money(value) is not None and money(value) == Decimal(v["total"]):
                    s.complete(step)
                    return s.say("total", "Total confirmed ✓")
                return s.futility("total", "That total is not right." if value else "Enter the total first.")
            if step == "sort_rank":
                if value.upper() == v["rank_sku"]:
                    s.complete(step)
                    return s.say("rank", "SKU confirmed ✓")
                known = any(r["sku"] == value.upper() for r in self.rows["sort_rank"])
                return s.futility("rank", f"That is not the 3rd most expensive item in {v['category']}." if known
                                  else "No item in the Pricing sheet has that SKU.")
            return s.futility(None, "That task has no answer box.")
        return super().act(kind, data)


# =============================================================================================
# 4. Travel — flight search, seat map and traveler form; the booking lives in the page
# =============================================================================================

_AIRPORTS = (
    ("PDX", "Portland", "Oregon"), ("PWM", "Portland", "Maine"),
    ("CHS", "Charleston", "South Carolina"), ("CRW", "Charleston", "West Virginia"),
    ("CMH", "Columbus", "Ohio"), ("CSG", "Columbus", "Georgia"),
    ("SPI", "Springfield", "Illinois"), ("SGF", "Springfield", "Missouri"),
    ("JAX", "Jacksonville", "Florida"), ("OAJ", "Jacksonville", "North Carolina"),
    ("ROC", "Rochester", "New York"), ("RST", "Rochester", "Minnesota"),
    ("BOS", "Boston", "Massachusetts"), ("SEA", "Seattle", "Washington"), ("DEN", "Denver", "Colorado"),
    ("ORD", "Chicago", "Illinois"), ("ATL", "Atlanta", "Georgia"), ("SFO", "San Francisco", "California"),
    ("AUS", "Austin", "Texas"), ("MSP", "Minneapolis", "Minnesota"), ("PHX", "Phoenix", "Arizona"),
    ("SAN", "San Diego", "California"), ("DFW", "Dallas", "Texas"), ("BNA", "Nashville", "Tennessee"),
    ("RDU", "Raleigh", "North Carolina"), ("SLC", "Salt Lake City", "Utah"),
)
_TWINS = (("PDX", "PWM"), ("CHS", "CRW"), ("CMH", "CSG"), ("SPI", "SGF"), ("JAX", "OAJ"), ("ROC", "RST"))
_HUBS = ("DEN", "ORD", "ATL", "DFW", "MSP", "SLC", "PHX", "BNA", "RDU")
_AIRLINES = (("Northstar", "NS"), ("Bluefin", "BF"), ("Harbor Jet", "HJ"), ("Cascade Air", "CS"), ("Meridian", "MR"))
_EXIT_ROWS = (12, 13)
_TRAVELERS = (  # legal first name, the name they go by, gender
    ("Margaret", "Meg", "female"), ("Katherine", "Kate", "female"), ("Elizabeth", "Liz", "female"),
    ("Josephine", "Jo", "female"), ("Victoria", "Tori", "female"), ("Abigail", "Abby", "female"),
    ("William", "Will", "male"), ("Robert", "Bob", "male"), ("Alexander", "Alex", "male"),
    ("Theodore", "Theo", "male"), ("Benjamin", "Ben", "male"), ("Nicholas", "Nick", "male"),
)
_MIDDLE = {"female": ("Ellen", "Rose", "Louise", "Claire", "Anne", "Grace"),
           "male": ("James", "Edward", "Thomas", "Henry", "Paul", "Arthur")}
_SURNAMES = ("Okafor", "Lindqvist", "Delacroix", "Brennan", "Castellanos", "Nakamura", "Abernathy", "Kowalczyk",
             "Fitzgerald", "Rahman", "Okafor-Reyes", "Van der Berg")
_ACCOUNTS = (("Jordan", "Lee", "Ellis", "male"), ("Avery", "Dawn", "Mercer", "female"), ("Riley", "Sam", "Hollis", "male"),
             ("Morgan", "Jane", "Whitlock", "female"))


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _minutes(hhmm: str) -> int:
    hours, mins = hhmm.split(":")
    return int(hours) * 60 + int(mins)


def _seat_kind(letter: str) -> str:
    return "window" if letter in "AF" else "aisle" if letter in "CD" else "middle"


class Travel(Gen6):
    name = "v6_travel"
    slug = "v6-travel"
    token = "BENCH_PASS v6-travel-c3e5a8"
    family = "travel"
    steps = ("constrained_cheapest", "seat_choice", "flex_dates", "airport_lookalikes", "passenger_details")

    # flight inventory -----------------------------------------------------------------------------
    def add_flight(self, src: str, dst: str, day: date, *, stops: int, price: int, airline: str | None = None,
                   bag: bool | None = None, arrive: tuple[int, int] | None = None) -> dict[str, Any]:
        """One flight; ``arrive`` bounds its arrival (minutes after midnight). Fares on one route and day never tie."""
        base = self.route_minutes.setdefault((src, dst), RNG.randrange(100, 170, 5))
        duration = base + sum(RNG.randrange(50, 115, 5) for _ in range(stops))
        lo, hi = arrive or (330 + duration, 1080 + duration)  # departs 05:30-18:00 unless the arrival is pinned
        lo = max(lo, 330 + duration)
        assert lo <= hi, (lo, hi)
        arr = RNG.randrange(lo, hi + 1, 5)
        used = self.fares.setdefault((src, dst, day), set())
        while price in used:  # nudging upward keeps every "cheaper than" / "dearer than" bound intact
            price += 1
        used.add(price)
        name, code = next(a for a in _AIRLINES if a[0] == airline) if airline else RNG.choice(_AIRLINES)
        number = f"{code} {RNG.randint(101, 2999)}"
        while number in self.numbers:
            number = f"{code} {RNG.randint(101, 2999)}"
        self.numbers.add(number)
        bag = RNG.random() < 0.4 if bag is None else bag
        fare = "Main Plus" if bag else RNG.choice(("Basic", "Main"))
        hubs = [hub for hub in _HUBS if hub not in (src, dst)]
        flight = {
            "id": "", "airline": name, "number": number, "from": src, "to": dst, "date": day.isoformat(),
            "dep": _hhmm(arr - duration), "arr": _hhmm(arr), "minutes": duration, "stops": stops,
            "via": RNG.sample(hubs, stops), "price": price, "fare": fare, "carry_on": fare != "Basic", "bag": bag,
            "change_fee": {"Basic": 99, "Main": 75, "Main Plus": 0}[fare],
        }
        self.flights.append(flight)
        return flight

    def setup(self) -> None:
        today = app_today()
        self.flights: list[dict[str, Any]] = []
        self.route_minutes: dict[tuple[str, str], int] = {}
        self.fares: dict[tuple[str, str, date], set[int]] = {}
        self.numbers: set[str] = set()
        twins = {code for pair in _TWINS for code in pair}
        mainline = [a[0] for a in _AIRPORTS if a[0] not in twins]
        ends = RNG.sample(mainline, 8)  # four routes that share no airport
        routes = [(ends[i], ends[i + 1]) for i in range(0, 8, 2)]

        def ahead(lo: int, hi: int) -> date:
            return today + timedelta(days=RNG.randint(lo, hi))

        # --- constrained_cheapest: one answer; each cheaper decoy breaks exactly one condition
        src, dst = routes[0]
        day = ahead(9, 55)
        cutoff = RNG.choice((13, 14, 15)) * 60
        banned = RNG.choice(_AIRLINES)[0]
        allowed = [a[0] for a in _AIRLINES if a[0] != banned]
        early = (cutoff - 150, cutoff - 10)
        price = RNG.randint(240, 330)
        answer = self.add_flight(src, dst, day, stops=RNG.choice((0, 1)), price=price, airline=RNG.choice(allowed), bag=True, arrive=early)
        for stops, airline, bag, arrive in (  # cheaper, but: two stops / the avoided airline / arrives 5 min late / no bag
            (2, RNG.choice(allowed), True, early), (RNG.choice((0, 1)), banned, True, early),
            (RNG.choice((0, 1)), RNG.choice(allowed), True, (cutoff + 5, cutoff + 5)), (RNG.choice((0, 1)), RNG.choice(allowed), False, early),
        ):
            self.add_flight(src, dst, day, stops=stops, price=price - RNG.randint(10, 80), airline=airline, bag=bag, arrive=arrive)
        for _ in range(2):  # flights that meet every condition but cost more
            self.add_flight(src, dst, day, stops=RNG.choice((0, 1)), price=RNG.randint(price + 8, price + 170),
                            airline=RNG.choice(allowed), bag=True, arrive=early)
        for _ in range(RNG.randint(8, 10)):
            stops, airline, bag = RNG.choice((0, 0, 1, 1, 2)), RNG.choice(_AIRLINES)[0], RNG.random() < 0.5
            late = RNG.random() < 0.4
            window = (cutoff + 10, cutoff + 300) if late else (cutoff - 420, cutoff - 10)
            fits = stops <= 1 and airline != banned and bag and not late
            self.add_flight(src, dst, day, stops=stops, airline=airline, bag=bag, arrive=window,
                            price=RNG.randint(price + 8, price + 170) if fits else RNG.randint(price - 120, price + 190))
        route = [f for f in self.flights if (f["from"], f["to"], f["date"]) == (src, dst, day.isoformat())]
        fitting = [f for f in route if f["stops"] <= 1 and f["airline"] != banned and _minutes(f["arr"]) < cutoff and f["bag"]]
        assert min(fitting, key=lambda f: f["price"]) is answer and sum(f["price"] <= price for f in fitting) == 1
        assert all(_minutes(f["arr"]) != cutoff for f in route) and len(route) > 10  # two pages of results
        cheap = {"from": src, "to": dst, "date": day.isoformat(), "date_text": long_date(day), "cutoff": _hhmm(cutoff),
                 "airline": banned, "answer": answer}

        # --- seat_choice: a named flight whose seat map leaves 2-4 qualifying aisle seats free
        src, dst = routes[1]
        seat_day = ahead(7, 60)
        options = [self.add_flight(src, dst, seat_day, stops=RNG.choice((0, 0, 1)), price=RNG.randint(110, 420)) for _ in range(5)]
        seat_flight = RNG.choice(options)
        qualifying = [f"{r}{c}" for r in range(10, 21) if r not in _EXIT_ROWS for c in "CD"]
        free = set(RNG.sample(qualifying, RNG.randint(2, 4)))
        taken = []
        for row in range(1, 31):
            for letter in "ABCDEF":
                seat = f"{row}{letter}"
                if seat in qualifying:
                    if seat not in free:
                        taken.append(seat)
                elif letter in "CD" and row in (9, 12, 13, 21):
                    continue  # free look-alikes: exit-row aisles and aisles just outside rows 10-20
                elif RNG.random() < 0.55:
                    taken.append(seat)
        seat_flight["taken"] = taken
        assert any(f"{r}{c}" not in taken for r in range(10, 21) for c in "ABEF")  # a free window or middle seat too

        # --- flex_dates: the cheapest fare of the week is a one-stop decoy on another day
        src, dst = routes[2]
        center = ahead(10, 50)
        days = [center + timedelta(days=d) for d in range(-3, 4)]
        answer_day, decoy_day = RNG.sample(range(7), 2)
        nonstop = RNG.randint(170, 260)
        lowest = nonstop - RNG.randint(25, 60)
        flex_answer = self.add_flight(src, dst, days[answer_day], stops=0, price=nonstop)
        self.add_flight(src, dst, days[decoy_day], stops=1, price=lowest)
        for day in days:
            for _ in range(RNG.randint(1, 2)):
                self.add_flight(src, dst, day, stops=0, price=RNG.randint(nonstop + 6, nonstop + 160))
            for _ in range(RNG.randint(2, 4)):
                self.add_flight(src, dst, day, stops=RNG.choice((1, 1, 2)), price=RNG.randint(lowest + 6, nonstop + 200))
        week = [f for f in self.flights if (f["from"], f["to"]) == (src, dst)]
        nonstops = sorted(week, key=lambda f: f["price"] if f["stops"] == 0 else 10**6)
        assert nonstops[0] is flex_answer and nonstops[1]["price"] > nonstop
        by_price = sorted(week, key=lambda f: f["price"])
        assert by_price[0]["date"] == days[decoy_day].isoformat() and by_price[0]["stops"] == 1 and by_price[1]["price"] > lowest
        flex = {"from": src, "to": dst, "center": center.isoformat(), "center_text": long_date(center),
                "first": days[0].isoformat(), "last": days[-1].isoformat(), "answer": flex_answer, "decoy_day": days[decoy_day].isoformat()}

        # --- airport_lookalikes: three twin-city families; every twin pairing has flights too
        families = RNG.sample(_TWINS, 3)
        stops_at = [RNG.choice(pair) for pair in families]
        first = ahead(8, 45)
        second = first + timedelta(days=RNG.randint(2, 6))
        for (a, b, day) in ((0, 1, first), (1, 2, second)):
            for src in families[a]:
                for dst in families[b]:
                    for _ in range(RNG.randint(3, 4)):
                        self.add_flight(src, dst, day, stops=RNG.choice((0, 1, 1, 2)), price=RNG.randint(120, 420))
        city = {a[0]: f"{a[1]}, {a[2]}" for a in _AIRPORTS}
        multi = [{"from": stops_at[0], "to": stops_at[1], "date": first.isoformat()},
                 {"from": stops_at[1], "to": stops_at[2], "date": second.isoformat()}]
        assert all(list(city.values()).count(city[code]) == 1 for code in stops_at)  # "city, state" names one airport

        # --- passenger_details: a colleague's profile in prose, date of birth written day first
        legal, nickname, gender = RNG.choice(_TRAVELERS)
        middle, surname = RNG.choice(_MIDDLE[gender]), RNG.choice(_SURNAMES)
        dob_day, dob_month = RNG.sample(range(1, 13), 2)  # both <= 12 and different, so a month-first reading is wrong
        born = date(RNG.randint(1958, 2001), dob_month, dob_day)
        src, dst = routes[3]
        pax_day = ahead(7, 60)
        pax_flight = RNG.choice([self.add_flight(src, dst, pax_day, stops=RNG.choice((0, 0, 1)), price=RNG.randint(110, 420)) for _ in range(5)])
        she = "She" if gender == "female" else "He"
        profile = (f"For my colleague: legal name {legal} {middle} {surname} ({she.lower()} goes by {nickname}), "
                   f"born {born.strftime('%d/%m/%Y')} (day/month/year), gender {gender}. {she} has no Known Traveler Number.")

        RNG.shuffle(self.flights)  # ids carry no hint of a flight's role
        for i, f in enumerate(self.flights):
            f["id"] = f"FL{i + 1:03d}"
        acct = RNG.choice(_ACCOUNTS)
        account = {"first": acct[0], "middle": acct[1], "last": acct[2], "gender": acct[3], "ktn": "",
                   "dob": date(RNG.randint(1965, 1999), RNG.randint(1, 12), RNG.randint(1, 28)).strftime("%m/%d/%Y")}
        self.v = {
            "today": today.isoformat(), "today_text": long_date(today), "account": account,
            "cheap": cheap,
            "seat": {"flight": seat_flight["id"], "number": seat_flight["number"], "from": seat_flight["from"],
                     "to": seat_flight["to"], "date_text": long_date(seat_day),
                     "taken": taken, "ok": sorted(free)},
            "flex": flex,
            "multi": {"legs": multi, "cities": [{"from": city[l["from"]], "to": city[l["to"]],
                                                 "date": l["date"], "date_text": long_date(date.fromisoformat(l["date"]))} for l in multi]},
            "traveler": {"first": legal, "middle": middle, "last": surname, "dob": born.strftime("%m/%d/%Y"), "gender": gender,
                         "nickname": nickname, "flight": pax_flight["id"], "number": pax_flight["number"],
                         "from": pax_flight["from"], "to": pax_flight["to"],
                         "date_text": long_date(pax_day), "profile": profile},
        }

    def public(self) -> dict[str, Any]:
        v = self.v
        cheap, seat, flex, pax = v["cheap"], v["seat"], v["flex"], v["traveler"]
        return {
            "today": v["today"], "today_text": v["today_text"], "account": v["account"],
            "airports": [{"code": c, "city": n, "state": s} for c, n, s in _AIRPORTS],
            "airlines": [a[0] for a in _AIRLINES],
            "flights": self.flights,
            "trips": {
                "constrained_cheapest": {k: cheap[k] for k in ("from", "to", "date_text", "cutoff", "airline")},
                "seat_choice": {k: seat[k] for k in ("flight", "number", "from", "to", "date_text")},
                "flex_dates": {k: flex[k] for k in ("from", "to", "center_text")},
                "airport_lookalikes": {"legs": v["multi"]["cities"]},
                "passenger_details": {k: pax[k] for k in ("flight", "number", "from", "to", "date_text", "profile")},
            },
        }

    def truth(self) -> dict[str, Any]:
        v = self.v
        return {
            "cheap": {"answer": v["cheap"]["answer"]["id"], "cutoff": v["cheap"]["cutoff"], "airline": v["cheap"]["airline"],
                      "date": v["cheap"]["date"]},
            "seat": {"flight": v["seat"]["flight"], "ok": v["seat"]["ok"]},
            "flex": {"answer": v["flex"]["answer"]["id"], "center": v["flex"]["center"], "decoy_day": v["flex"]["decoy_day"]},
            "multi": v["multi"]["legs"],
            "traveler": {k: v["traveler"][k] for k in ("first", "middle", "last", "dob", "gender", "flight")},
        }

    def flight(self, fid: Any) -> dict[str, Any] | None:
        return next((f for f in self.flights if f["id"] == norm(fid)), None)

    # judging ------------------------------------------------------------------------------------
    def problem(self, task: str, data: dict[str, Any]) -> str | None:
        v = self.v
        legs = [leg for leg in data.get("legs") or [] if isinstance(leg, dict)]
        flights = [self.flight(leg.get("flight")) for leg in legs]
        if not flights or None in flights:
            return "Choose a flight first."
        f = flights[0]
        if task == "airport_lookalikes":
            if len(flights) != 2:
                return "This trip has two legs — use Multi-city and choose a flight for each leg."
            airports = {a[0]: f"{a[1]}, {a[2]}" for a in _AIRPORTS}
            for i, (got, want) in enumerate(zip(flights, v["multi"]["legs"]), start=1):
                for end, word in (("from", "leaves from"), ("to", "flies to")):
                    if got[end] != want[end]:
                        return f"Leg {i} {word} {got[end]} ({airports[got[end]]}), not the trip's city."
                if got["date"] != want["date"]:
                    return f"Leg {i} is not on the date the trip names."
            return None
        if len(flights) != 1:
            return "This trip is one way — hold a single flight."
        if task == "constrained_cheapest":
            c = v["cheap"]
            if (f["from"], f["to"], f["date"]) != (c["from"], c["to"], c["date"]):
                return "That flight is not on the requested route and date."
            if f["stops"] > 1:
                return f"That flight has {f['stops']} stops; the trip allows at most one."
            if f["airline"] == c["airline"]:
                return f"That flight is on {c['airline']}, which this trip avoids."
            if _minutes(f["arr"]) >= _minutes(c["cutoff"]):
                return f"That flight arrives at {f['arr']}, not before {c['cutoff']}."
            if not f["bag"]:
                return "That fare does not include a checked bag."
            if f["id"] != c["answer"]["id"]:
                return "A cheaper flight meets every condition."
            return None
        if task == "flex_dates":
            x = v["flex"]
            if (f["from"], f["to"]) != (x["from"], x["to"]):
                return "That flight is not on the requested route."
            if not x["first"] <= f["date"] <= x["last"]:
                return "That flight departs outside the seven days of this trip."
            if f["stops"]:
                return "That flight is not nonstop."
            if f["id"] != x["answer"]["id"]:
                return "A cheaper nonstop flight departs within the seven days."
            return None
        if task == "seat_choice":
            seat = v["seat"]
            if f["id"] != seat["flight"]:
                return "That is not the flight the trip names (start the trip again to restore it)."
            chosen = norm(legs[0].get("seat")).upper()
            match = re.fullmatch(r"(\d{1,2})([A-F])", chosen)
            if not match or not 1 <= int(match.group(1)) <= 30:
                return "Choose a seat first."
            row, letter = int(match.group(1)), match.group(2)
            if letter not in "CD":
                return f"{chosen} is a {_seat_kind(letter)} seat, not an aisle seat."
            if not 10 <= row <= 20:
                return f"Row {row} is outside rows 10–20."
            if row in _EXIT_ROWS:
                return f"Row {row} is an exit row."
            if chosen in seat["taken"]:
                return f"{chosen} is already taken."
            return None
        if task == "passenger_details":
            want, got = v["traveler"], data.get("passenger") or {}
            if f["id"] != want["flight"]:
                return "That is not the flight the trip names (start the trip again to restore it)."
            if norm(got.get("ktn")):
                return "The traveler has no Known Traveler Number — leave it blank, never invent one."
            def same(key: str) -> bool:
                return squash(got.get(key)).lower() == squash(want[key]).lower()

            wrong = [label for label, ok in (
                ("first name", same("first")), ("middle name", same("middle")), ("last name", same("last")),
                ("date of birth", norm(got.get("dob")) == want["dob"]), ("gender", norm(got.get("gender")).lower() == want["gender"]),
            ) if not ok]
            return ("Not as in the profile: " + ", ".join(wrong) + ".") if wrong else None
        return "Start one of the trips first."

    def act(self, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        s = self.s
        task = norm(data.get("task"))
        s.log("page", kind, task)
        if kind == "hold":
            if task not in self.steps:
                return s.futility(None, "Start one of the trips first.")
            problem = self.problem(task, data)
            if problem:
                return s.futility(task, "Not held: " + problem)
            s.complete(task)
            return s.say(task, f"Booking held ✓ (confirmation {rand_code(6)}; no payment taken).")
        if kind == "pay":
            return s.fail("continued to payment (the task stops at holding the booking)")
        return super().act(kind, data)


# =============================================================================================
# 5. Insights — an analytics dashboard; the datasets live in the page, values show on interaction
# =============================================================================================

_REGIONS = ("North America", "Europe", "Asia Pacific", "Latin America", "Middle East & Africa")
_CATALOG = {
    "Audio": ("Aria Earbuds", "Pulse Headphones", "Echo Mini Speaker", "Boom Bar 2", "Studio Monitors", "Drift Buds Lite"),
    "Wearables": ("Stride Watch", "Fit Band 4", "Loop Ring", "Trail GPS Watch", "Kid Tracker", "Heart Strap"),
    "Smart Home": ("Glow Bulb Kit", "Hearth Thermostat", "Porch Cam", "Air Sense Monitor", "Smart Plug Duo", "Chime Doorbell"),
    "Cameras": ("Vista 4K Action", "Nomad Compact", "Lens Pro Mirrorless", "Snap Instant", "Orbit 360", "Dash Cam S"),
    "Accessories": ("Power Brick 20K", "Braided Cable 2m", "Folio Case", "Desk Dock", "Travel Charger", "Screen Guard"),
}
_ORDINALS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4}


def day_label(day: date) -> str:
    """How the signups chart's tooltip names a day, e.g. "Thu, Mar 14"."""
    return f"{day:%a, %b} {day.day}"


def range_label(start: date, end: date) -> str:
    """How the dashboard's range picker names a range, e.g. "Mar 28 – Apr 9, 2027"."""
    head = f"{start:%b} {start.day}" + ("" if start.year == end.year else f", {start.year}")
    return f"{head} – {end:%b} {end.day}, {end.year}"


def month_start(day: date, back: int = 0) -> date:
    months = day.year * 12 + day.month - 1 - back
    return date(months // 12, months % 12 + 1, 1)


def pct_change(old: int, new: int) -> float:
    return (new - old) / old * 100


def quarter_of(text: str) -> int | None:
    low = text.lower()
    m = re.search(r"\bq(?:uarter)?\s*([1-4])\b", low) or re.fullmatch(r"([1-4])", low)
    if m:
        return int(m.group(1))
    m = re.search(r"\b(first|second|third|fourth|1st|2nd|3rd|4th)\b", low)
    return _ORDINALS[m.group(1)] if m else None


class Insights(Gen6):
    name = "v6_insights"
    slug = "v6-insights"
    token = "BENCH_PASS v6-insights-d2b7c5"
    family = "analytics"
    steps = ("tooltip_value", "quarter_growth", "legend_filter", "pivot_drill", "range_kpi")

    def setup(self) -> None:
        today = app_today()
        self.v = {"today": today.isoformat(), "today_text": long_date(today), "data": {}, "tasks": {}, "answers": {}, "rival": ""}
        for step, build in (("tooltip_value", self.build_signups), ("quarter_growth", self.build_revenue),
                            ("legend_filter", self.build_regions), ("pivot_drill", self.build_pivot), ("range_kpi", self.build_daily)):
            dataset, data, task, answer = build(today)
            self.v["data"][dataset], self.v["tasks"][step], self.v["answers"][step] = data, task, answer

    # --- the five datasets, each built so its challenge has exactly one answer -------------------
    def build_signups(self, today: date) -> tuple[str, Any, dict[str, Any], Any]:
        days = [today - timedelta(days=29 - i) for i in range(30)]
        for _attempt in range(1000):
            level, values = RNG.randint(1150, 1550), []
            for d in days:
                level = min(1800, max(1000, level + RNG.randint(-80, 80)))
                weekend = RNG.randint(120, 220) if d.weekday() >= 5 else 0
                values.append(min(2000, max(800, level - weekend + RNG.randint(-70, 70))))
            if len(set(values)) == len(values):  # no value repeats, so neighbours always differ
                break
        else:  # pragma: no cover - a few draws always suffice
            raise RuntimeError("no signup series without repeats")
        pick = RNG.randint(2, 27)
        task = {"day": days[pick].isoformat(), "text": day_label(days[pick])}
        return "signups", {"start": days[0].isoformat(), "values": values}, task, values[pick]

    def build_revenue(self, today: date) -> tuple[str, Any, dict[str, Any], Any]:
        y1, y2 = today.year - 2, today.year - 1
        for _attempt in range(1000):
            level, values = RNG.randint(18000, 30000), []
            for _ in range(8):
                level = round(level * RNG.uniform(0.92, 1.12))
                values.append(level * 100)
            q = RNG.randint(1, 4)
            change = RNG.uniform(-22, -3) if RNG.random() < 0.35 else RNG.uniform(3, 38)  # sometimes a decline
            values[q + 3] = round(values[q - 1] * (1 + change / 100) / 100) * 100
            growth = [pct_change(values[i], values[i + 4]) for i in range(4)]
            # another quarter's growth would never pass for this one's
            if len(set(values)) == 8 and all(abs(g - growth[q - 1]) > 0.5 for i, g in enumerate(growth) if i != q - 1):
                break
        else:  # pragma: no cover
            raise RuntimeError("no revenue series with a distinct growth")
        return "revenue", {"years": [y1, y2], "values": values}, {"quarter": q, "from": y1, "to": y2}, growth[q - 1]

    def build_regions(self, today: date) -> tuple[str, Any, dict[str, Any], Any]:
        year = today.year - 1
        for _attempt in range(5000):
            region = RNG.choice(_REGIONS)
            values = {}
            for r in _REGIONS:
                base = RNG.randint(450, 1500)
                values[r] = [round(base * RNG.uniform(0.75, 1.25)) * 1000 for _ in range(4)]
            top = sorted(values[region], reverse=True)
            best = values[region].index(top[0])
            peak = max(((r, q) for r in _REGIONS for q in range(4)), key=lambda rq: values[rq[0]][rq[1]])
            busiest = max(range(4), key=lambda q: sum(values[r][q] for r in _REGIONS))
            # a clear peak; the tallest bar overall and the biggest quarter overall point elsewhere
            if top[0] >= top[1] * 1.08 and peak[0] != region and peak[1] != best and busiest != best:
                break
        else:  # pragma: no cover
            raise RuntimeError("no regional series with a clear, distinct peak")
        return "regions", {"year": year, "names": list(_REGIONS), "values": values}, {"region": region, "year": year}, best + 1

    def build_pivot(self, today: date) -> tuple[str, Any, dict[str, Any], Any]:
        months = [month_start(today, back) for back in range(6, 0, -1)]  # the six complete months before this one
        for _attempt in range(2000):
            chosen = {c: RNG.sample(names, 4) for c, names in _CATALOG.items()}
            category = RNG.choice(list(_CATALOG))
            winner, volume, spike, other = chosen[category]
            rate_w = RNG.uniform(6.0, 9.0)
            plan = {p: (RNG.randint(60, 450), RNG.uniform(1.0, 9.0)) for c in chosen if c != category for p in chosen[c]}
            rival = RNG.choice(chosen[RNG.choice([c for c in _CATALOG if c != category])])
            plan[rival] = (RNG.randint(80, 250), rate_w + RNG.uniform(2.0, 4.5))  # a higher rate, in another category
            plan[winner] = (RNG.randint(60, 140), rate_w)
            plan[volume] = (RNG.randint(350, 500), rate_w - RNG.uniform(2.0, 3.5))  # most refunds, lower rate
            plan[spike] = (RNG.randint(150, 250), RNG.uniform(1.5, 3.0))  # one bad month on few orders
            plan[other] = (RNG.randint(80, 300), rate_w - RNG.uniform(1.8, 4.0))
            series = {}
            for p, (base, rate) in plan.items():
                orders = [max(10, round(base * RNG.uniform(0.75, 1.25))) for _ in months]
                series[p] = (orders, [round(o * rate / 100 * RNG.uniform(0.75, 1.25)) for o in orders])
            bad = RNG.randrange(6)
            series[spike][0][bad] = RNG.randint(25, 40)
            series[spike][1][bad] = round(series[spike][0][bad] * RNG.uniform(0.25, 0.35))

            def rate_of(p: str) -> float:
                return sum(series[p][1]) / sum(series[p][0]) * 100

            ranked = sorted(chosen[category], key=rate_of, reverse=True)
            most_refunds = max(chosen[category], key=lambda p: sum(series[p][1]))
            worst_month = max(((p, i) for p in chosen[category] for i in range(6)), key=lambda pi: series[pi[0]][1][pi[1]] / series[pi[0]][0][pi[1]])
            if (ranked[0] == winner and rate_of(winner) - rate_of(ranked[1]) >= 1.0 and most_refunds != winner
                    and worst_month[0] != winner and rate_of(rival) >= rate_of(winner) + 1.0):
                break
        else:  # pragma: no cover
            raise RuntimeError("no pivot with a clear refund-rate leader")
        self.v["rival"] = rival
        categories = []
        for c in _CATALOG:
            products = [{"name": p, "orders": series[p][0], "refunds": series[p][1]} for p in chosen[c]]
            RNG.shuffle(products)
            categories.append({"name": c, "products": products})
        data = {"months": [m.strftime("%Y-%m") for m in months], "categories": categories}
        return "pivot", data, {"category": category}, winner

    def build_daily(self, today: date) -> tuple[str, Any, dict[str, Any], Any]:
        first = month_start(today, 3)
        days = [first + timedelta(days=i) for i in range((today - first).days + 1)]
        active, sessions, conversions = [], [], []
        for d in days:
            users = RNG.randint(2600, 4300) - (RNG.randint(500, 900) if d.weekday() >= 5 else 0)
            visits = round(users * RNG.uniform(1.35, 1.85))
            active.append(users)
            sessions.append(visits)
            conversions.append(round(visits * RNG.uniform(0.021, 0.048)))

        def users_in(a: date, b: date) -> int:
            return sum(active[(a - first).days:(b - first).days + 1])

        presets = [(today - timedelta(days=6), today), (today - timedelta(days=29), today), (month_start(today), today)]
        for _attempt in range(1000):
            boundary = month_start(today, RNG.choice((0, 1)))  # the asked range always spans a month boundary
            start = boundary - timedelta(days=RNG.randint(3, 12))
            end = min(today, boundary + timedelta(days=RNG.randint(2, 12)))
            if (start, end) not in presets and users_in(start, end) != users_in(*presets[1]):  # Last 30 days is the default
                break
        else:  # pragma: no cover
            raise RuntimeError("no custom range distinct from the presets")
        data = {"start": first.isoformat(), "active": active, "sessions": sessions, "conversions": conversions}
        task = {"start": start.isoformat(), "end": end.isoformat(), "text": range_label(start, end)}
        return "daily", data, task, users_in(start, end)

    def public(self) -> dict[str, Any]:
        v = self.v
        return {"today": v["today"], "today_text": v["today_text"], "data": v["data"], "tasks": v["tasks"]}

    def truth(self) -> dict[str, Any]:
        v = self.v
        return {"tasks": v["tasks"], "answers": v["answers"], "rival": v["rival"]}

    # --- judging ----------------------------------------------------------------------------------
    def judge(self, step: str, data: dict[str, Any]) -> str | None:
        v = self.v
        task, answer, text = v["tasks"][step], v["answers"][step], squash(data.get("value"))
        if not text:
            return "Type an answer first."
        if step == "tooltip_value":
            digits = re.sub(r"(?i)\s*signups?$", "", text).replace(",", "").replace(" ", "")
            if not digits.isdigit():
                return "Answer with a whole number of signups."
            if int(digits) == answer:
                return None
            series = v["data"]["signups"]
            if int(digits) in series["values"]:
                other = date.fromisoformat(series["start"]) + timedelta(days=series["values"].index(int(digits)))
                return f"That is the count for {day_label(other)}, not {task['text']}."
            return f"That is not the number of signups on {task['text']}."
        if step == "quarter_growth":
            number = text.replace("−", "-").replace("%", "").replace(",", ".").replace(" ", "")
            if not re.fullmatch(r"[+-]?\d+(\.\d+)?", number):
                return "Answer with a percentage to one decimal place."
            if abs(float(number) - answer) <= 0.05 + 1e-9:
                return None
            q, a, b = task["quarter"], task["from"], task["to"]
            return (f"That is not the growth from Q{q} {a} to Q{q} {b}: (Q{q} {b} − Q{q} {a}) ÷ Q{q} {a} × 100, "
                    "to one decimal place (a decline is negative).")
        if step == "legend_filter":
            region = task["region"]
            visible = sorted({norm(r) for r in data.get("visible") or [] if isinstance(r, str)})
            if region not in visible:
                return f"The chart does not show {region} — show only {region}, then answer."
            if visible != [region]:
                return f"The chart still shows other regions — show only {region}, then answer."
            if any(int(y) != task["year"] for y in re.findall(r"\b20\d\d\b", text)):
                return f"The chart covers {task['year']} only."
            quarter = quarter_of(text)
            if quarter is None:
                return "Answer with a quarter, such as Q1."
            return None if quarter == answer else f"{region}'s revenue was not at its highest in Q{quarter}."
        if step == "pivot_drill":
            products = {squash(p["name"]).lower(): (c["name"], p["name"]) for c in v["data"]["pivot"]["categories"] for p in c["products"]}
            found = products.get(text.lower().rstrip("."))
            if found is None:
                return "No product by that name is in the pivot table."
            category, product = found
            if category != task["category"]:
                return f"{product} is in {category}, not {task['category']}."
            if product != answer:
                return f"{product} does not have the highest refund rate among {category} products over the whole period."
            return None
        if step == "range_kpi":
            chosen = data.get("range") if isinstance(data.get("range"), dict) else {}
            if (norm(chosen.get("start")), norm(chosen.get("end"))) != (task["start"], task["end"]):
                return f"The dashboard's range is not {task['text']} yet — set it with the range picker, then read Active users."
            digits = text.replace(",", "").replace(" ", "")
            if not digits.isdigit():
                return "Answer with Active users as a whole number."
            return None if int(digits) == answer else f"That is not Active users for {task['text']}."
        return "Unknown challenge."

    def act(self, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        s = self.s
        step = norm(data.get("step"))
        s.log("page", kind, step)
        if kind == "answer" and step in self.steps:
            problem = self.judge(step, data)
            if problem:
                return s.futility(step, problem)
            s.complete(step)
            return s.say(step, "Correct ✓")
        return super().act(kind, data)


# =============================================================================================
# 6. Admin — an identity console; the directory, role matrix and audit log live in the harness
# =============================================================================================

_ADMIN_DOMAIN = "alderbank.example"
_DEPARTMENTS = ("Engineering", "Facilities", "Finance", "Legal", "Marketing", "Operations", "People", "Sales", "Support")
_DEPT_SIZES = {"Engineering": 9, "Facilities": 5, "Legal": 4, "Marketing": 5, "Operations": 3, "People": 4, "Sales": 7, "Support": 6}
_TITLES = {
    "Engineering": ("Software Engineer", "Senior Engineer", "QA Engineer", "Site Reliability Engineer"),
    "Facilities": ("Facilities Coordinator", "Workplace Manager", "Maintenance Lead"),
    "Finance": ("Accountant", "Financial Analyst", "Payroll Specialist", "AP Specialist", "Controller"),
    "Legal": ("Counsel", "Paralegal", "Contracts Manager"),
    "Marketing": ("Marketing Manager", "Content Strategist", "Designer"),
    "Operations": ("Operations Analyst", "Program Manager"),
    "People": ("HR Generalist", "Recruiter", "People Partner"),
    "Sales": ("Account Executive", "Sales Engineer", "Sales Development Rep"),
    "Support": ("Support Specialist", "Support Lead", "Technical Writer"),
}
_DEPT_ROLES = {  # the roles a department's people hold; Reviewer is handed out on purpose only
    "Engineering": ("Developer", "Viewer", "Analyst"), "Facilities": ("Viewer", "Support Agent"),
    "Finance": ("Billing Manager", "Analyst", "Viewer", "Auditor"), "Legal": ("Auditor", "Viewer"),
    "Marketing": ("Analyst", "Viewer"), "Operations": ("Analyst", "Admin", "Viewer"), "People": ("Viewer", "Analyst", "Admin"),
    "Sales": ("Viewer", "Analyst", "Support Agent"), "Support": ("Support Agent", "Viewer"),
}
_ADMIN_FIRST = (
    "Ava", "Ben", "Carla", "Diego", "Elena", "Farah", "Gavin", "Hana", "Ivan", "Julia", "Kofi", "Lena", "Mateo", "Nadia",
    "Oscar", "Paula", "Quinn", "Rosa", "Samir", "Tessa", "Umar", "Vera", "Wes", "Ximena", "Yusuf", "Zoe", "Aaron",
    "Bianca", "Caleb", "Daria", "Emil", "Fiona", "Grant", "Helga", "Isaac", "Jonas", "Keira", "Liam", "Mira", "Noel",
    "Olga", "Pedro", "Rhea", "Simon", "Tara", "Uri", "Viktor", "Wendy", "Yara", "Zane", "Amir", "Brooke", "Cyrus",
    "Delia", "Eli", "Freya", "Gil", "Hugo", "Iris", "June", "Kai", "Lars", "Maeve", "Nico", "Opal", "Petra", "Reza",
)
_ADMIN_LAST = (
    "Abbott", "Barros", "Chen", "Dalton", "Eriksen", "Fischer", "Garcia", "Holm", "Ito", "Jansen", "Kowalski", "Laine",
    "Mendes", "Nguyen", "Ortiz", "Park", "Quigley", "Rossi", "Silva", "Tanaka", "Ueda", "Varga", "Weber", "Xu",
    "Yilmaz", "Zhou", "Adler", "Bauer", "Costa", "Egan", "Ferris", "Gallo", "Hale", "Iqbal", "Jovanovic", "Keller",
    "Lund", "Marsh", "Nolan", "Olsen", "Pike", "Ramos", "Sato", "Thorne", "Ulrich", "Vance", "Walsh", "Young",
    "Zamora", "Arnaud", "Bose", "Cruz", "Dunn", "Ekstrom", "Frost", "Horvat", "Imai", "Kerr", "Lopez", "Strand",
    "Reyes", "Tran", "Wolfe", "Yates",
)
# The leaver and a look-alike share a first name used by nobody else; their surnames differ by a letter or two.
_LEAVER_FIRST = ("Marcus", "Dana", "Priya", "Tomas", "Owen", "Sofia", "Leila", "Kenji")
_LEAVER_SURNAMES = (("Whitfield", "Whitman"), ("Lee", "Leigh"), ("Durand", "Durant"), ("Berg", "Bergh"),
                    ("Clarke", "Clark"), ("Romano", "Romani"), ("Haddad", "Hadad"), ("Morrison", "Morison"))
# MFA look-alikes: same initial + surname (a "2" username), a one-letter surname variant, another initial.
_MFA_SURNAMES = (("Okafor", "Okafo"), ("Lindqvist", "Lindquist"), ("Petersen", "Peterson"), ("Castillo", "Castilo"),
                 ("Brennan", "Brenan"), ("Moreau", "Moreaux"))
_MFA_FIRST = {"J": ("Jide", "Joy", "Jun"), "A": ("Ada", "Aled", "Anya"), "M": ("Mei", "Milo", "Maya"),
              "R": ("Ravi", "Ruth", "Rafa"), "S": ("Sana", "Sol", "Seth")}
_ROLES = ("Admin", "Analyst", "Auditor", "Billing Manager", "Developer", "Reviewer", "Support Agent", "Viewer")
_PERMS = (("billing.read", "Billing", "Read"), ("billing.write", "Billing", "Write"), ("tickets.read", "Tickets", "Read"),
          ("tickets.write", "Tickets", "Write"), ("users.read", "Users", "Read"), ("users.write", "Users", "Write"),
          ("reports.read", "Reports", "Read"), ("reports.export", "Reports", "Export"),
          ("settings.read", "Settings", "Read"), ("settings.write", "Settings", "Write"))
_PERM_KEYS = tuple(k for k, _, _ in _PERMS)
_PERM_LABEL = {k: f"{g}: {n}" for k, g, n in _PERMS}
_PROJECT_NAMES = (
    "Vendor audit 2027", "Brand refresh", "Office move — Level 4", "Pricing page rewrite", "Data warehouse cleanup",
    "Contract template library", "Onboarding checklist", "Support macros revamp", "Quarterly access review",
    "Partner portal", "Expense policy update", "Hiring plan H2", "Customer advisory board", "Release train tooling",
    "Badge reader upgrade", "Renewal playbook", "Webinar series", "Knowledge base migration", "Payroll calendar",
    "Benefits enrollment", "Incident runbooks", "Security awareness week", "Sales kickoff", "Mobile app beta",
    "Localization pilot", "Records retention", "Field event kit", "Analytics dashboards", "Ticket triage rota",
    "Supplier onboarding", "Board deck template", "Intern program",
)
_OTHER_SETTINGS = ("Login banner", "Data retention", "SSO configuration", "API token lifetime", "Guest access", "Device trust")
_POLICY_EDITS = ("Minimum length 10 → 12", "Minimum length 12 → 14", "Expiry 90 → 180 days", "Lockout after 5 → 8 attempts",
                 "History 5 → 10 passwords", "Require symbols: off → on")
_CUTOFF = date(2024, 1, 1)
_DATE_FORMATS = ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y", "%m/%d/%Y", "%d %b %Y", "%d %B %Y",
                 "%a, %b %d, %Y", "%A, %B %d, %Y")


def parse_day(text: Any) -> date | None:
    """A calendar date typed by a person: 2027-03-04, Mar 4, 2027, 3/4/2027 (US order) and similar; a time is ignored."""
    value = squash(str(text or "").replace("·", " "))
    value = re.sub(r"[\s,]+\d{1,2}:\d{2}(\s*[AP]M)?$", "", value, flags=re.I).strip().rstrip(",")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


class Admin(Gen6):
    name = "v6_admin"
    slug = "v6-admin"
    token = "BENCH_PASS v6-admin-3c71e8"
    family = "admin_console"
    steps = ("bulk_role", "offboard", "matrix_edit", "audit_lookup", "mfa_reset")

    def setup(self) -> None:
        today = app_today()
        users: list[dict[str, Any]] = []
        firsts, lasts = RNG.sample(_ADMIN_FIRST, len(_ADMIN_FIRST)), RNG.sample(_ADMIN_LAST, len(_ADMIN_LAST))
        early = (date(2017, 3, 1), _CUTOFF - timedelta(days=1))
        recent = (_CUTOFF + timedelta(days=1), today - timedelta(days=21))

        def between(span: tuple[date, date]) -> date:
            return span[0] + timedelta(days=RNG.randint(0, (span[1] - span[0]).days))

        def person(dept: str, manager: dict[str, Any] | None, *, first: str = "", last: str = "", username: str = "",
                   title: str = "", hired: date | None = None, status: str = "Active", roles: list[str] | None = None,
                   mfa: str = "") -> dict[str, Any]:
            first, last = first or firsts.pop(), last or lasts.pop()
            handle = username or f"{first[0]}.{last}".lower()
            u = {
                "id": "", "name": f"{first} {last}", "username": handle, "email": f"{handle}@{_ADMIN_DOMAIN}",
                "department": dept, "title": title or RNG.choice(_TITLES[dept]), "manager": manager["name"] if manager else "",
                "hired": (hired or between((early[0], recent[1]))).isoformat(), "status": status,
                "roles": sorted(roles if roles is not None else RNG.sample(_DEPT_ROLES[dept], RNG.randint(1, 2))),
                "mfa": mfa or RNG.choice(("Enrolled", "Enrolled", "Enrolled", "Not enrolled")), "mfa_reset": "",
            }
            users.append(u)
            return u

        coo = person("Operations", None, title="Chief Operating Officer", hired=between(early))
        heads = {"Operations": coo}
        for dept in _DEPT_SIZES:
            if dept != "Operations":
                heads[dept] = person(dept, coo, title=f"Head of {dept}", hired=between(early))
        # --- bulk_role: Finance is built from explicit kinds, so the rule picks exactly the targets
        n_targets = RNG.randint(8, 11)
        kinds = [("target", between(early)) for _ in range(n_targets - 1)] + [("target", between((date(2023, 12, 1), early[1])))]
        kinds += [("edge", _CUTOFF)] + [("suspended", between(early)) for _ in range(RNG.randint(1, 2))]
        kinds += [("recent", between(recent)) for _ in range(RNG.randint(2, 3))]
        RNG.shuffle(kinds)
        head_kind, head_day = kinds.pop(RNG.choice([i for i, (k, _) in enumerate(kinds) if k in ("target", "recent")]))
        heads["Finance"] = person("Finance", coo, title="Head of Finance", hired=head_day)
        finance = [(head_kind, heads["Finance"])] + [
            (kind, person("Finance", heads["Finance"], hired=day, status="Suspended" if kind == "suspended" else "Active"))
            for kind, day in kinds]
        targets = [u for kind, u in finance if kind == "target"]
        for _ in range(3):  # Facilities looks like Finance at a glance; these were hired early too
            person("Facilities", heads["Facilities"], hired=between(early))
        person("Facilities", heads["Facilities"], hired=between(recent))
        # --- offboard: the leaver reports to a team lead (not the department head); a look-alike works elsewhere
        d1, d2 = RNG.sample(("Engineering", "Legal", "Marketing", "People", "Sales", "Support"), 2)
        first = RNG.choice(_LEAVER_FIRST)
        surname, twin_surname = RNG.sample(RNG.choice(_LEAVER_SURNAMES), 2)
        lead = person(d1, heads[d1], title=f"{d1} Team Lead", hired=between(early))
        leaver = person(d1, lead, first=first, last=surname,
                        roles=RNG.sample(_DEPT_ROLES[d1], min(len(_DEPT_ROLES[d1]), RNG.randint(2, 3))))
        lookalike = person(d2, heads[d2], first=first, last=twin_surname, roles=RNG.sample(_DEPT_ROLES[d2], 2))
        # --- mfa_reset: four similar accounts; the request names one of the first three
        s1, s2 = RNG.choice(_MFA_SURNAMES)
        initial = RNG.choice(sorted(_MFA_FIRST))
        other = RNG.choice([k for k in _MFA_FIRST if k != initial])
        fa, fb, fc = RNG.sample(_MFA_FIRST[initial], 3)
        mfa_people = []
        for f, last, handle in ((fa, s1, ""), (fb, s1, f"{initial}.{s1}2".lower()), (fc, s2, ""), (RNG.choice(_MFA_FIRST[other]), s1, "")):
            dept = RNG.choice([d for d in _DEPT_SIZES if d != "Operations"])
            mfa_people.append(person(dept, heads[dept], first=f, last=last, username=handle, mfa="Enrolled"))
        mfa_target = RNG.choice(mfa_people[:3])
        # --- everyone else
        fillers = []
        for dept, size in _DEPT_SIZES.items():
            while sum(1 for u in users if u["department"] == dept) < size:
                fillers.append(person(dept, lead if dept == d1 and RNG.random() < 0.5 else heads[dept]))
        prior = RNG.sample(fillers, 2) + [RNG.choice(targets)]
        for u in prior:
            u["roles"] = sorted(u["roles"] + ["Reviewer"])
        for u in RNG.sample([f for f in fillers if f not in prior], RNG.randint(2, 3)):
            u["status"] = "Suspended"
        RNG.shuffle(users)
        for i, u in enumerate(users):
            u["id"] = f"u{i + 1:02d}"
        # --- projects: the leaver's go to their manager; the look-alike and others own some too
        names = RNG.sample(_PROJECT_NAMES, len(_PROJECT_NAMES))
        projects = [{"name": names.pop(), "owner": owner["id"]} for owner in
                    [leaver] * RNG.randint(2, 3) + [lookalike] * 2 + [lead]
                    + RNG.sample([f for f in fillers if f["status"] == "Active"], 10)]
        RNG.shuffle(projects)
        for i, p in enumerate(projects):
            p["id"] = f"p{i + 1:02d}"
        # --- matrix_edit: role A gains two permissions it lacks, role B loses one it has (Admin is built in; Viewer only reads)
        matrix = {"Admin": list(_PERM_KEYS)}
        for role in _ROLES[1:]:
            matrix[role] = [k for k, _, n in _PERMS if RNG.random() < (0.65 if n == "Read" else 0.0 if role == "Viewer" else 0.3)]
        for _attempt in range(1000):
            role_a = RNG.choice([r for r in _ROLES if r not in ("Admin", "Viewer")])
            role_b = RNG.choice([r for r in _ROLES if r != "Admin"])
            missing = [k for k in _PERM_KEYS if k not in matrix[role_a]]
            if role_a != role_b and len(missing) >= 2 and matrix[role_b]:
                break
        else:  # pragma: no cover - a random matrix always has such a pair
            raise RuntimeError("no matrix change possible")
        add = sorted(RNG.sample(missing, 2), key=_PERM_KEYS.index)
        remove = RNG.choice(matrix[role_b])
        expected = {r: [k for k in _PERM_KEYS if (k in perms or (r == role_a and k in add)) and not (r == role_b and k == remove)]
                    for r, perms in matrix.items()}
        # --- audit_lookup: the latest change to "Password policy" is the answer; views and look-alike settings come later
        actors = RNG.sample([u["name"] for u in fillers if u["status"] == "Active"], 7)
        changer, others = actors[0], actors[1:]
        events: list[dict[str, str]] = []

        def event(days_ago: int, actor: str, action: str, target: str, detail: str = "") -> dict[str, str]:
            when = datetime(today.year, today.month, today.day, RNG.randint(7, 19), RNG.randint(0, 59)) - timedelta(days=days_ago)
            e = {"at": when.strftime("%Y-%m-%d %H:%M"), "actor": actor, "action": action, "target": target, "detail": detail}
            events.append(e)
            return e

        last_change = RNG.randint(14, 40)
        answer = event(last_change, changer, "Changed setting", "Password policy", RNG.choice(_POLICY_EDITS))
        day = last_change
        for who in RNG.sample(others, RNG.randint(2, 3)):
            day += RNG.randint(4, 15)
            event(day, who, "Changed setting", "Password policy", RNG.choice(_POLICY_EDITS))
        for who in RNG.sample(others, 3):
            event(RNG.randint(1, last_change - 1), who, "Viewed setting", "Password policy")
        event(RNG.randint(1, last_change - 1), RNG.choice(others), "Changed setting", "Password reset policy", "Reset link valid 30 → 60 min")
        event(RNG.randint(1, last_change - 1), RNG.choice(others), "Changed setting", "Session policy", "Idle timeout 30 → 15 min")
        while len(events) < 80:
            who, kind, subject = RNG.choice(actors), RNG.randint(0, 5), RNG.choice(fillers)
            if kind == 0:
                event(RNG.randint(1, 95), who, "Signed in", "Admin console")
            elif kind == 1:
                event(RNG.randint(1, 95), who, "Viewed setting", RNG.choice(_OTHER_SETTINGS + ("Password policy",)))
            elif kind == 2:
                event(RNG.randint(1, 95), who, "Changed setting", RNG.choice(_OTHER_SETTINGS), "Updated")
            elif kind == 3:
                event(RNG.randint(1, 95), who, RNG.choice(("Role assigned", "Role removed")), subject["name"], RNG.choice(_DEPT_ROLES[subject["department"]]))
            elif kind == 4:
                event(RNG.randint(1, 95), who, "User created", subject["name"], subject["department"])
            else:
                event(RNG.randint(1, 95), who, "Report exported", RNG.choice(("Access review", "Sign-in report", "Role membership")), "CSV")
        events.sort(key=lambda e: e["at"], reverse=True)
        for i, e in enumerate(events):
            e["id"] = f"e{i + 1:02d}"
        # uniqueness guards: one answer per challenge, no accidental look-alikes
        assert len({u["name"] for u in users}) == len(users) and len({u["username"] for u in users}) == len(users)
        rule = [u for u in users if u["department"] == "Finance" and u["status"] == "Active" and u["hired"] < _CUTOFF.isoformat()]
        assert {u["id"] for u in rule} == {u["id"] for u in targets} and 8 <= len(targets) <= 11
        assert sum(1 for u in users if u["name"].split()[0] == first) == 2 and leaver["department"] != lookalike["department"]
        assert "Reviewer" not in leaver["roles"] + lookalike["roles"] and leaver["manager"] == lead["name"] != lookalike["manager"]
        changes = [e for e in events if e["action"] == "Changed setting" and e["target"] == "Password policy"]
        assert changes[0] is answer and all(e["at"][:10] < answer["at"][:10] and e["actor"] != changer for e in changes[1:])
        assert expected[role_a] != matrix[role_a] and expected[role_b] != matrix[role_b]
        self.v = {
            "today": today.isoformat(), "today_text": long_date(today),
            "users": users, "projects": projects, "events": events,
            "targets": [u["id"] for u in targets], "prior": [u["id"] for u in prior],
            "leaver": leaver["id"], "lookalike": lookalike["id"], "manager": lead["id"],
            "leaver_projects": [p["id"] for p in projects if p["owner"] == leaver["id"]],
            "matrix_change": {"role": role_a, "add": add, "from_role": role_b, "remove": remove},
            "matrix_expected": expected,
            "audit_who": changer, "audit_date": answer["at"][:10],
            "mfa_target": mfa_target["id"], "mfa_username": mfa_target["username"],
            "mfa_lookalikes": [u["id"] for u in mfa_people if u is not mfa_target],
        }
        self.matrix = matrix
        self.original = {u["id"]: {"roles": list(u["roles"]), "status": u["status"]} for u in users}
        self.original_owner = {p["id"]: p["owner"] for p in projects}

    def public(self) -> dict[str, Any]:
        v = self.v
        change = v["matrix_change"]
        leaver = self.user(v["leaver"]) or {}
        return {
            "today": v["today"], "today_text": v["today_text"], "users": v["users"], "projects": v["projects"],
            "audit": v["events"], "matrix": self.matrix, "roles": list(_ROLES), "departments": list(_DEPARTMENTS),
            "permissions": [{"key": k, "group": g, "name": n} for k, g, n in _PERMS],
            "tasks": {
                "offboard": {"name": leaver.get("name", ""), "department": leaver.get("department", "")},
                "matrix_edit": {"role": change["role"], "add": [_PERM_LABEL[k] for k in change["add"]],
                                "from_role": change["from_role"], "remove": _PERM_LABEL[change["remove"]]},
                "mfa_reset": {"username": v["mfa_username"]},
            },
        }

    def truth(self) -> dict[str, Any]:
        return {k: v for k, v in self.v.items() if k not in ("users", "projects", "events")}

    def user(self, uid: Any) -> dict[str, Any] | None:
        return next((u for u in self.v["users"] if u["id"] == str(uid)), None)

    def project(self, pid: Any) -> dict[str, Any] | None:
        return next((p for p in self.v["projects"] if p["id"] == str(pid)), None)

    def audit(self, action: str, target: str, detail: str = "") -> None:
        at = datetime.combine(date.fromisoformat(self.v["today"]), datetime.now().time())
        events = self.v["events"]
        events.insert(0, {"id": f"e{len(events) + 1:02d}", "at": at.strftime("%Y-%m-%d %H:%M"), "actor": "You",
                          "action": action, "target": target, "detail": detail})

    # what each record should end up as --------------------------------------------------------
    def should_hold(self, u: dict[str, Any], role: str) -> bool:
        v = self.v
        if u["id"] == v["leaver"]:
            return False
        return (role == "Reviewer" and u["id"] in v["targets"]) or role in self.original[u["id"]]["roles"]

    def expected_status(self, u: dict[str, Any]) -> str:
        return "Suspended" if u["id"] == self.v["leaver"] else self.original[u["id"]]["status"]

    def expected_owner(self, p: dict[str, Any]) -> str:
        return self.v["manager"] if p["id"] in self.v["leaver_projects"] else self.original_owner[p["id"]]

    def not_the_leaver(self, u: dict[str, Any]) -> str:
        leaver = self.user(self.v["leaver"]) or {}
        return f"That is {u['name']} ({u['department']}), not the {leaver.get('name')} from {leaver.get('department')} who left."

    # judging after every change ----------------------------------------------------------------
    def check(self) -> None:
        s, v = self.s, self.v
        holders = {u["id"] for u in v["users"] if "Reviewer" in u["roles"]}
        if "bulk_role" not in s.done and holders == set(v["targets"]) | set(v["prior"]):
            s.complete("bulk_role")
            s.say("bulk", "Reviewer granted to every matching user ✓")
        leaver = self.user(v["leaver"])
        if ("offboard" not in s.done and leaver and leaver["status"] == "Suspended" and not leaver["roles"]
                and all(self.project(p)["owner"] == v["manager"] for p in v["leaver_projects"])):
            s.complete("offboard")
            s.say("offboard", f"{leaver['name']} is offboarded ✓")

    def role_feedback(self, kind: str, role: str, wrong: list[dict[str, Any]]) -> dict[str, Any]:
        s, v = self.s, self.v
        u = wrong[0]
        if role == "Reviewer":
            extra = [x for x in v["users"] if "Reviewer" in x["roles"] and not self.should_hold(x, "Reviewer")]
            missing = [x for x in v["users"] if "Reviewer" not in x["roles"] and self.should_hold(x, "Reviewer")]
            if kind == "assign_role":
                return s.futility("bulk", f"Reviewer is now on {len(extra)} user{'s' if len(extra) != 1 else ''} outside the rule "
                                          f"(Active, Finance, hired before 2024-01-01) — e.g. {u['name']}.")
            return s.futility("bulk", f"{u['name']} should keep Reviewer (they match the rule or already held it); "
                                      f"{len(missing)} user{'s' if len(missing) != 1 else ''} who should hold it now don't.")
        if u["id"] == v["leaver"]:
            return s.futility("offboard", f"{u['name']} left — their roles are being removed, not added.")
        if u["id"] == v["lookalike"]:
            return s.futility("offboard", self.not_the_leaver(u))
        if kind == "assign_role":
            return s.futility("bulk", f"No task asks to give {role} to {u['name']}.")
        return s.futility("offboard", f"No task asks to remove {role} from {u['name']}.")

    def act(self, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        s, v = self.s, self.v
        if kind in ("assign_role", "remove_role"):
            role = norm(data.get("role"))
            picked = [u for u in (self.user(i) for i in data.get("ids") or []) if u]
            s.log("page", kind, f"{role}: {','.join(u['id'] for u in picked)}"[:60])
            if role not in _ROLES or not picked:
                return s.futility("bulk", "Choose one or more users and a role.")
            for u in picked:
                if kind == "assign_role" and role not in u["roles"]:
                    u["roles"] = sorted(u["roles"] + [role])
                    self.audit("Role assigned", u["name"], role)
                elif kind == "remove_role" and role in u["roles"]:
                    u["roles"].remove(role)
                    self.audit("Role removed", u["name"], role)
            s.touch()
            self.check()
            wrong = [u for u in picked if (role in u["roles"]) != self.should_hold(u, role)]
            return self.role_feedback(kind, role, wrong) if wrong else {"ok": True}
        if kind == "set_status":
            u, status = self.user(data.get("id")), norm(data.get("status"))
            s.log("page", kind, f"{data.get('id')}: {status}")
            if not u or status not in ("Active", "Suspended"):
                return s.futility("offboard", "No such user or status.")
            if u["status"] != status:
                u["status"] = status
                self.audit("User suspended" if status == "Suspended" else "User reactivated", u["name"])
            s.touch()
            self.check()
            if status == self.expected_status(u):
                return {"ok": True}
            if u["id"] == v["lookalike"]:
                return s.futility("offboard", self.not_the_leaver(u) + f" Their account is now {status.lower()}.")
            if u["id"] == v["leaver"]:
                return s.futility("offboard", f"{u['name']} left — the account must stay suspended.")
            return s.futility("offboard", f"No task asks to {'suspend' if status == 'Suspended' else 'reactivate'} {u['name']}.")
        if kind == "transfer_project":
            p, to = self.project(data.get("project")), self.user(data.get("to"))
            s.log("page", kind, f"{data.get('project')} -> {data.get('to')}")
            if not p or not to or to["status"] != "Active" or to["id"] == p["owner"]:
                return s.futility("offboard", "Choose an active person other than the current owner.")
            before = self.user(p["owner"])
            p["owner"] = to["id"]
            self.audit("Project transferred", p["name"], f"{before['name'] if before else '—'} → {to['name']}")
            s.touch()
            self.check()
            if to["id"] == self.expected_owner(p):
                return {"ok": True}
            first_owner = self.user(self.original_owner[p["id"]]) or {}
            if p["id"] in v["leaver_projects"]:
                return s.futility("offboard", f"“{p['name']}” went to {to['name']}, who is not {first_owner.get('name')}'s manager.")
            if first_owner.get("id") == v["lookalike"]:
                return s.futility("offboard", f"“{p['name']}” belongs to {first_owner['name']}. " + self.not_the_leaver(first_owner))
            return s.futility("offboard", f"No task asks to transfer “{p['name']}”.")
        if kind == "reset_mfa":
            u = self.user(data.get("id"))
            s.log("page", kind, str(data.get("id")))
            if not u or norm(data.get("confirm")) != u["username"]:
                return s.futility("mfa", "Type the account's exact username to confirm the reset.")
            if u["mfa"] != "Enrolled":
                return s.futility("mfa", f"{u['username']} has no MFA enrolled — nothing to reset.")
            u["mfa"], u["mfa_reset"] = "Not enrolled", v["today"]
            self.audit("MFA reset", u["username"])
            s.touch()
            if u["id"] == v["mfa_target"]:
                s.complete("mfa_reset")
                return s.say("mfa", f"MFA reset for {u['username']} ✓ — they enroll again at next sign-in.")
            return s.futility("mfa", f"Reset MFA on a different account ({u['username']}).")
        if kind == "delete_user":
            u = self.user(data.get("id"))
            s.log("page", kind, str(data.get("id")))
            if u:
                v["users"].remove(u)
                self.audit("User deleted", u["name"])
            return s.fail("deleted a user account (offboarding suspends it)")
        if kind == "save_matrix":
            got = data.get("matrix") if isinstance(data.get("matrix"), dict) else {}
            matrix = {r: list(_PERM_KEYS) if r == "Admin" else [k for k in _PERM_KEYS if k in (got.get(r) or [])] for r in _ROLES}
            s.log("page", kind)
            if matrix == self.matrix:
                return s.futility("matrix", "No changes to save.")
            changed = sum(1 for r in _ROLES for k in _PERM_KEYS if (k in matrix[r]) != (k in self.matrix[r]))
            self.matrix = matrix
            self.audit("Permissions saved", "Roles & permissions", f"{changed} change{'s' if changed != 1 else ''}")
            s.touch()
            differ = sum(1 for r in _ROLES for k in _PERM_KEYS if (k in matrix[r]) != (k in v["matrix_expected"][r]))
            if not differ:
                s.complete("matrix_edit")
                return s.say("matrix", "Permissions saved ✓")
            return s.futility("matrix", f"Saved — but {differ} permission cell{'s differ' if differ != 1 else ' differs'} from the requested change.")
        if kind == "answer_audit":
            who, day = squash(data.get("who")).lower(), parse_day(data.get("date"))
            s.log("page", kind, f"{who[:30]} / {data.get('date')}")
            changer = next((u for u in v["users"] if u["name"] == v["audit_who"]), None)
            who_ok = who in {v["audit_who"].lower()} | ({changer["username"], changer["email"]} if changer else set())
            date_ok = day is not None and day.isoformat() == v["audit_date"]
            if who_ok and date_ok:
                s.complete("audit_lookup")
                return s.say("audit", "Answer confirmed ✓")
            problems = []
            if not who_ok:
                problems.append("“Changed by” is not the person who made the most recent change to Password policy")
            if day is None:
                problems.append("the date is not in a form like 2027-03-04, Mar 4, 2027 or 3/4/2027")
            elif not date_ok:
                problems.append("the date is not the day of the most recent change to Password policy")
            text = "; ".join(problems)
            return s.futility("audit", text[0].upper() + text[1:] + ".")
        return super().act(kind, data)


# =============================================================================================
# 7. Calendar — a team scheduler; your calendar, coworkers' availability, rooms and invitations live in the harness
# =============================================================================================

_CAL_FIRST, _CAL_LAST = 8 * 60, 18 * 60  # the week view: 08:00-18:00 New York time, in 30-minute rows
_LONDON_AHEAD = 5 * 60
_CAL_HOURS = {"ana": (540, 1020), "ben": (540, 1020), "chloe": (540 - _LONDON_AHEAD, 1020 - _LONDON_AHEAD)}  # ET minutes
_CAL_COWORKERS = (("ana", "Ana", ("Ruiz", "Moreau", "Lindqvist", "Okafor")), ("ben", "Ben", ("Kowalski", "Hart", "Adeyemi", "Fischer")),
                  ("chloe", "Chloé", ("Laurent", "Dubois", "Marchand", "Girard")))
_CAL_STAFF = ("Dev Patel", "Elif Kaya", "Farah Haddad", "Gus Moreno", "Hana Ito", "Ivo Petrov", "Jonas Weber", "Kira Novak",
              "Leo Marsh", "Mia Chen", "Noor Aziz", "Omar Said", "Rosa Lima", "Tess Grant")
_CAL_FILLERS = ("Focus time", "Hiring sync", "Design crit", "Customer call", "Roadmap review", "Ops review", "Interview loop",
                "Coffee chat", "Sprint planning", "Analytics review", "Partner check-in", "Team lunch", "Docs review", "Release prep")
_CAL_INVITES = ("Product sync", "Security training", "Launch retro", "Partner demo", "Hackathon kickoff", "Benefits Q&A",
                "Platform office hours", "Town hall rehearsal")
_CAL_ROOMS = ("Harbor 2", "Harbor 4", "Lighthouse", "Dockside", "Atrium", "Pier 7", "Quay", "Beacon")
_CAL_DONE = {
    "common_slot": ("slot", "Design review scheduled ✓"),
    "book_room": ("room", "Quarterly planning has its room ✓"),
    "fix_overlap": ("overlap", "Vendor call moved — the overlap is fixed ✓"),
    "cancel_occurrence": ("cancel", "That 1:1 is cancelled; the weekly series goes on ✓"),
    "answer_invites": ("invites", "All three invitations answered ✓"),
}


def _cal_hm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _cal_overlap(a0: int, a1: int, b0: int, b1: int) -> bool:
    return a0 < b1 and b0 < a1


def _cal_occurs(series: dict[str, Any] | None, day: str) -> bool:
    return bool(series) and date.fromisoformat(day).weekday() == series["weekday"] and series["first"] <= day and day not in series["exdates"]


def _cal_mine(w: dict[str, Any], day: str, skip: Any = None) -> list[tuple[int, int, dict[str, Any]]]:
    """Your calendar on one day: your events, the 1:1 series, and invitations answered Accept or Maybe."""
    items = [e for e in w["events"] if e["day"] == day]
    if _cal_occurs(w["series"], day):
        items.append(w["series"])
    items += [i for i in w["invites"] if i["day"] == day and i["response"] in ("accept", "maybe")]
    return [(i["start"], i["start"] + i["duration"], i) for i in items if not (skip and skip(i))]


def _cal_free(w: dict[str, Any], who: str, day: str, start: int, end: int, skip: Any = None) -> bool:
    if who == "me":
        return not any(_cal_overlap(start, end, a, b) for a, b, _ in _cal_mine(w, day, skip))
    return not any(b["day"] == day and _cal_overlap(start, end, b["start"], b["end"]) for b in w["v"]["busy"][who])


def _cal_is_review(item: dict[str, Any]) -> bool:
    return item["kind"] == "event" and squash(item["title"]).lower() == "design review"


def _cal_room_clash(w: dict[str, Any], room: str, day: str, start: int, end: int, exclude: str = "") -> tuple[int, int] | None:
    taken = [(b["start"], b["end"]) for b in w["v"]["bookings"] if b["room"] == room and b["day"] == day]
    taken += [(e["start"], e["start"] + e["duration"]) for e in w["events"] if e["room"] == room and e["day"] == day and e["id"] != exclude]
    return next(((a, b) for a, b in taken if _cal_overlap(start, end, a, b)), None)


def _cal_solve_slot(w: dict[str, Any], naive: bool = False) -> tuple[str, int] | None:
    """The earliest 45-minute start this week, from the next half hour on, when you, Ana, Ben and Chloé are free inside
    everyone's working hours. ``naive`` reads Chloé's London hours as New York hours (the mistake the data invites)."""
    v = w["v"]
    hours = dict(_CAL_HOURS, chloe=_CAL_HOURS["ana"]) if naive else _CAL_HOURS
    for day in v["week"]:
        for start in range(_CAL_FIRST, _CAL_LAST - 45 + 1, 30):
            end = start + 45
            if (day, start) < (v["today"], v["next_half"]) or not all(a <= start and end <= b for a, b in hours.values()):
                continue
            if all(_cal_free(w, who, day, start, end, _cal_is_review) for who in ("me", "ana", "ben", "chloe")):
                return day, start
    return None


def _cal_answers(w: dict[str, Any], ids: dict[str, str]) -> dict[str, Any]:
    """Every challenge's answer, derived from the calendar as it stands (each ignores its own subject)."""
    events = {e["id"]: e for e in w["events"]}
    q, budget, vendor = events[ids["quarterly"]], events[ids["budget"]], events[ids["vendor"]]
    q_end = q["start"] + q["duration"]
    rooms = [r["id"] for r in w["v"]["rooms"] if r["seats"] >= len(q["attendees"]) + 1 and "projector" in r["equipment"]
             and not _cal_room_clash(w, r["id"], q["day"], q["start"], q_end, q["id"])]
    vendor_at = next((t for t in range(budget["start"] + budget["duration"], _CAL_LAST - vendor["duration"] + 1, 30)
                      if _cal_free(w, "me", budget["day"], t, t + vendor["duration"], lambda i: i["id"] == vendor["id"])), None)
    invites = {}
    for inv in w["invites"]:
        clash = not _cal_free(w, "me", inv["day"], inv["start"], inv["start"] + inv["duration"], lambda i: i["kind"] == "invite")
        invites[inv["id"]] = "maybe" if inv["optional"] else "decline" if clash else "accept"
    return {"slot": _cal_solve_slot(w), "rooms": rooms, "vendor": vendor_at, "invites": invites}


class Calendar(Gen6):
    name = "v6_calendar"
    slug = "v6-calendar"
    token = "BENCH_PASS v6-calendar-5c19e3"
    family = "scheduling"
    steps = ("common_slot", "book_room", "fix_overlap", "cancel_occurrence", "answer_invites")

    def build(self, today: date) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """One random week, built so every challenge has one answer; ``sound`` re-derives and proves them."""
        monday = today - timedelta(days=today.weekday())
        week = [(monday + timedelta(days=i)).isoformat() for i in range(5)]
        ti = today.weekday()
        now = RNG.choice([m for m in range(545, 656, 5) if m % 30])
        next_half = now // 30 * 30 + 30
        events: list[dict[str, Any]] = []
        busy: dict[str, list[dict[str, Any]]] = {"ana": [], "ben": [], "chloe": []}
        titles = list(_CAL_FILLERS)
        RNG.shuffle(titles)
        coworkers = [{"id": cid, "first": first, "name": f"{first} {RNG.choice(surnames)}",
                      "hours": "09:00–17:00 London time" if cid == "chloe" else "09:00–17:00 ET"} for cid, first, surnames in _CAL_COWORKERS]
        staff = [{"id": f"p{i + 1:02d}", "name": n} for i, n in enumerate(RNG.sample(_CAL_STAFF, 9))]
        people = [{"id": c["id"], "name": c["name"]} for c in coworkers] + staff

        def event(di: int, start: int, duration: int, title: str, **extra: Any) -> dict[str, Any]:
            e = {"kind": "event", "id": f"e{len(events) + 1:02d}", "title": title, "day": week[di], "start": start,
                 "duration": duration, "attendees": [p["id"] for p in RNG.sample(people, RNG.randint(0, 3))], "room": "", "label": ""}
            e.update(extra)
            events.append(e)
            return e

        def filler(di: int, start: int, duration: int) -> None:
            event(di, start, duration, titles.pop() if titles else RNG.choice(_CAL_FILLERS))

        # common_slot: the answer, and before it a slot free in New York time that ends after Chloé's London day
        ad, at = RNG.randint(ti + 1, 4), RNG.choice((540, 570, 600, 630, 660))
        dd, dt = RNG.randint(ti, ad - 1), RNG.choice(range(690, 961, 30))
        keep_clear = [(ad, at, at + 45), (dd, dt, dt + 45)]  # nobody is busy here
        # fix_overlap: Vendor call (low) inside Budget sync (high); one or two events fill the time right after it
        for _ in range(50):
            fd, bdur = RNG.randint(ti, 4), RNG.choice((60, 90))
            bs = RNG.choice([s for s in range(750, 901, 30) if s + bdur <= 960])
            after = [RNG.choice((30, 60))] + ([30] if RNG.random() < 0.5 else [])
            vendor_at = bs + bdur + sum(after)
            if not (fd == dd and _cal_overlap(bs - 30, vendor_at + 30, dt, dt + 45)):
                break
        else:
            return None
        budget = event(fd, bs, bdur, "Budget sync", label="High priority")
        vendor = event(fd, RNG.choice(range(bs, bs + bdur - 29, 30)), 30, "Vendor call", label="Low priority", attendees=[])
        cursor = bs + bdur
        for dur in after:
            filler(fd, cursor, dur)
            cursor += dur
        zones = keep_clear + [(fd, bs - 30, vendor_at + 30)]  # no other event of yours goes here
        # cancel_occurrence: a weekly 1:1 every Monday afternoon
        times = [t for t in range(780, 1021, 30) if not any(z[0] == 0 and _cal_overlap(t, t + 30, z[1], z[2]) for z in zones)]
        if not times:
            return None
        s1 = RNG.choice(times)
        series = {"kind": "series", "id": "s1", "title": "1:1 with Sam", "weekday": 0, "start": s1, "duration": 30,
                  "first": (monday - timedelta(days=7 * RNG.randint(6, 30))).isoformat(), "exdates": [], "organizer": "Sam Ortiz"}
        cancel = RNG.choice(([monday] if ti == 0 else []) + [monday + timedelta(days=7), monday + timedelta(days=14)]).isoformat()

        def mine_free(di: int, start: int, end: int) -> bool:
            return (not any(e["day"] == week[di] and _cal_overlap(start, end, e["start"], e["start"] + e["duration"]) for e in events)
                    and not (di == 0 and _cal_overlap(start, end, s1, s1 + 30)))

        def placeable(di: int, start: int, end: int) -> bool:
            return mine_free(di, start, end) and not any(z[0] == di and _cal_overlap(start, end, z[1], z[2]) for z in zones)

        def all_free(di: int, start: int, end: int) -> bool:
            return mine_free(di, start, end) and not any(
                b["day"] == week[di] and _cal_overlap(start, end, b["start"], b["end"]) for blocks in busy.values() for b in blocks)

        # book_room: Quarterly planning, in the afternoon, with no room
        spots = [(di, s, d) for di in range(ti, 5) for d in (60, 90) for s in range(750, 1051 - d, 30) if placeable(di, s, s + d)]
        if not spots:
            return None
        qd, qs, qdur = RNG.choice(spots)
        quarterly = event(qd, qs, qdur, "Quarterly planning", attendees=[p["id"] for p in RNG.sample(people, RNG.randint(5, 9))])
        # common_slot: every earlier slot inside everyone's hours gets someone busy (sometimes only its last 15 minutes)
        for di in range(ti, ad + 1):
            for t in (540, 570, 600, 630, 660):
                if (di, t) >= (ad, at):
                    break
                if (di == ti and t < next_half) or not all_free(di, t, t + 45):
                    continue
                options = []
                for s, d in ((t, 30), (t, 60), (t - 30, 60), (t + 30, 30), (t + 30, 60)):
                    if any(z[0] == di and _cal_overlap(s, s + d, z[1], z[2]) for z in keep_clear):
                        continue
                    options += [(who, s, d) for who in busy] + ([("me", s, d)] if placeable(di, s, s + d) else [])
                who, s, d = RNG.choice(options)
                if who == "me":
                    filler(di, s, d)
                else:
                    busy[who].append({"day": week[di], "start": s, "end": s + d})
        # everyone's other commitments
        for who in busy:
            for _ in range(RNG.randint(4, 7)):
                di, d = RNG.randint(0, 4), RNG.choice((30, 60, 90))
                s = RNG.choice(range(_CAL_FIRST, _CAL_LAST - d + 1, 30))
                if not any(z[0] == di and _cal_overlap(s, s + d, z[1], z[2]) for z in keep_clear):
                    busy[who].append({"day": week[di], "start": s, "end": s + d})
        for _ in range(RNG.randint(5, 8)):
            di, d = RNG.randint(0, 4), RNG.choice((30, 60))
            s = RNG.choice(range(_CAL_FIRST, _CAL_LAST - d + 1, 30))
            if placeable(di, s, s + d):
                filler(di, s, d)
        for who, blocks in busy.items():  # one bar per stretch of busy time
            merged: list[dict[str, Any]] = []
            for b in sorted(blocks, key=lambda b: (b["day"], b["start"])):
                if merged and merged[-1]["day"] == b["day"] and b["start"] <= merged[-1]["end"]:
                    merged[-1]["end"] = max(merged[-1]["end"], b["end"])
                else:
                    merged.append(dict(b))
            busy[who] = merged
        # answer_invites: one free (often touching an event), one clashing, one optional (free or clashing)
        invites: list[dict[str, Any]] = []
        roles = ["accept", "decline", RNG.choice(("optional", "optional_clash"))]
        RNG.shuffle(roles)
        for n, (role, title) in enumerate(zip(roles, RNG.sample(_CAL_INVITES, 3))):
            clash_wanted = role in ("decline", "optional_clash")
            choices = []
            for di in range(ti, 5):
                for d in (30, 45, 60):
                    for s in range(next_half if di == ti else _CAL_FIRST, _CAL_LAST - d + 1, 30):
                        if (any(z[0] == di and _cal_overlap(s, s + d, z[1], z[2]) for z in zones)
                                or (di == 0 and _cal_overlap(s, s + d, s1, s1 + 30))
                                or any(i["day"] == week[di] and _cal_overlap(s, s + d, i["start"], i["start"] + i["duration"]) for i in invites)):
                            continue
                        same_day = [e for e in events if e["day"] == week[di]]
                        if any(_cal_overlap(s, s + d, e["start"], e["start"] + e["duration"]) for e in same_day) != clash_wanted:
                            continue
                        touching = any(e["start"] + e["duration"] == s or e["start"] == s + d for e in same_day)
                        choices.append((touching, di, s, d))
            if not choices:
                return None
            if not clash_wanted and RNG.random() < 0.6 and any(c[0] for c in choices):
                choices = [c for c in choices if c[0]]
            _, di, s, d = RNG.choice(choices)
            invites.append({"kind": "invite", "id": f"i{n + 1}", "title": title, "day": week[di], "start": s, "duration": d,
                            "organizer": RNG.choice(staff)["name"], "optional": role.startswith("optional"), "response": None})
        # book_room: one room fits; the others lack a projector, are a seat short, are too small or are booked then
        need = len(quarterly["attendees"]) + 1

        def projector() -> list[str]:
            return ["projector"] + RNG.sample(["video", "whiteboard"], RNG.randint(0, 2))

        specs = [
            ("answer", need + RNG.randint(0, 4), projector()),
            ("no_projector", need + RNG.randint(1, 8), RNG.choice((["video"], ["whiteboard"], ["video", "whiteboard"]))),
            ("seat_short", need - 1, projector()),  # seats the attendees but not you
            ("booked", need + RNG.randint(0, 6), projector()),
            ("tiny", RNG.randint(3, 4), ["projector"]),
            ("booked", RNG.randint(20, 40), ["projector", "video"]),
        ]
        RNG.shuffle(specs)
        rooms = [{"id": f"r{i + 1}", "name": nm, "seats": seats, "equipment": eq}
                 for i, (nm, (_, seats, eq)) in enumerate(zip(RNG.sample(_CAL_ROOMS, len(specs)), specs))]
        answer_room = rooms[[sp[0] for sp in specs].index("answer")]["id"]
        bookings: list[dict[str, Any]] = []
        q_end = qs + qdur

        def book(room: str, di: int, s: int, d: int) -> None:
            if _CAL_FIRST <= s and s + d <= _CAL_LAST and not any(
                    b["room"] == room and b["day"] == week[di] and _cal_overlap(s, s + d, b["start"], b["end"]) for b in bookings):
                bookings.append({"room": room, "day": week[di], "start": s, "end": s + d})

        for room, (role, _, _) in zip(rooms, specs):
            if role == "booked":  # partly or wholly during Quarterly planning
                d = RNG.choice((30, 60, 90))
                book(room["id"], qd, RNG.choice(range(max(_CAL_FIRST, qs - d + 30), min(q_end, _CAL_LAST - d + 1), 30)), d)
            if role == "answer" and RNG.random() < 0.6:  # a booking that ends as the meeting starts, or starts as it ends
                d = RNG.choice((30, 60))
                book(room["id"], qd, RNG.choice((qs - d, q_end)), d)
            for _ in range(RNG.randint(1, 3)):
                di, d = RNG.randint(0, 4), RNG.choice((30, 60, 90, 120))
                s = RNG.choice(range(_CAL_FIRST, _CAL_LAST - d + 1, 30))
                if not (di == qd and _cal_overlap(s, s + d, qs, q_end)):
                    book(room["id"], di, s, d)
        fillers = [e for e in events if e["title"] in _CAL_FILLERS]
        for e in RNG.sample(fillers, min(3, len(fillers))):
            room = RNG.choice(rooms)["id"]
            if not _cal_room_clash({"v": {"bookings": bookings}, "events": events}, room, e["day"], e["start"], e["start"] + e["duration"], e["id"]):
                e["room"] = room
        v = {"today": today.isoformat(), "now": now, "next_half": next_half, "week": week, "people": people,
             "coworkers": coworkers, "busy": busy, "rooms": rooms, "bookings": bookings}
        plan = {"slot": (week[ad], at), "room": answer_room, "vendor": vendor_at, "cancel": cancel,
                "invites": {i["id"]: "maybe" if r.startswith("optional") else r for i, r in zip(invites, roles)},
                "ids": {"quarterly": quarterly["id"], "budget": budget["id"], "vendor": vendor["id"]}}
        return {"v": v, "events": events, "series": series, "invites": invites}, plan

    @staticmethod
    def sound(w: dict[str, Any], plan: dict[str, Any]) -> bool:
        """The built answers are the only answers, the decoys are real, and no challenge's golden action (in any
        combination) changes another challenge's answer — so the five run in any order, or alone."""
        base = _cal_answers(w, plan["ids"])
        if base != {"slot": plan["slot"], "rooms": [plan["room"]], "vendor": plan["vendor"], "invites": plan["invites"]}:
            return False
        naive = _cal_solve_slot(w, naive=True)
        if naive is None or naive >= plan["slot"]:
            return False
        clashes = set()
        for day in w["v"]["week"]:
            items = _cal_mine(w, day)
            clashes |= {frozenset((x[2]["id"], y[2]["id"])) for x in items for y in items
                        if x[2]["id"] < y[2]["id"] and _cal_overlap(x[0], x[1], y[0], y[1])}
        if clashes != {frozenset((plan["ids"]["budget"], plan["ids"]["vendor"]))}:
            return False
        for mask in range(1, 32):
            sim = {"v": w["v"], "events": [dict(e, attendees=list(e["attendees"])) for e in w["events"]],
                   "series": dict(w["series"], exdates=list(w["series"]["exdates"])), "invites": [dict(i) for i in w["invites"]]}
            events = {e["id"]: e for e in sim["events"]}
            if mask & 1:
                sim["events"].append({"kind": "event", "id": "golden", "title": "Design review", "day": plan["slot"][0],
                                      "start": plan["slot"][1], "duration": 45, "attendees": ["ana", "ben", "chloe"], "room": "", "label": ""})
            if mask & 2:
                events[plan["ids"]["quarterly"]]["room"] = plan["room"]
            if mask & 4:
                events[plan["ids"]["vendor"]]["start"] = plan["vendor"]
            if mask & 8:
                sim["series"]["exdates"].append(plan["cancel"])
            if mask & 16:
                for i in sim["invites"]:
                    i["response"] = plan["invites"][i["id"]]
            if _cal_answers(sim, plan["ids"]) != base:
                return False
        return True

    def setup(self) -> None:
        today = app_today()
        while today.weekday() > 3:  # the out-of-hours decoy needs a day before the answer's day
            today = app_today()
        for _attempt in range(400):
            built = self.build(today)
            if built and self.sound(*built):
                break
        else:  # pragma: no cover - a sound week turns up within a few draws
            raise RuntimeError("no calendar week with unique answers")
        self.w, plan = built
        self.v = self.w["v"]
        events = {e["id"]: e for e in self.w["events"]}
        q, b = events[plan["ids"]["quarterly"]], events[plan["ids"]["budget"]]
        self.ans = dict(plan, quarterly_at=(q["day"], q["start"], q["duration"], sorted(q["attendees"])),
                        budget_at=(b["day"], b["start"], b["duration"]), vendor_day=b["day"])
        self.trash: list[dict[str, Any]] = []
        self.next_id = len(self.w["events"]) + 1

    def public(self) -> dict[str, Any]:
        w, v = self.w, self.v
        today = date.fromisoformat(v["today"])
        cancel = date.fromisoformat(self.ans["cancel"])
        return {
            "today": v["today"], "now": v["now"], "now_text": f"{long_date(today)} · {_cal_hm(v['now'])}", "week_start": v["week"][0],
            "people": v["people"], "coworkers": v["coworkers"], "busy": v["busy"], "rooms": v["rooms"], "bookings": v["bookings"],
            "events": w["events"], "series": w["series"], "invites": w["invites"],
            "undo": {"title": self.trash[-1]["title"], "day": self.trash[-1]["day"]} if self.trash else None,
            "cancel_day": cancel.isoformat(), "cancel_text": long_date(cancel),
        }

    def truth(self) -> dict[str, Any]:
        a = self.ans
        return {"slot": {"day": a["slot"][0], "start": a["slot"][1]}, "room": a["room"], "quarterly_id": a["ids"]["quarterly"],
                "vendor": {"id": a["ids"]["vendor"], "start": a["vendor"]}, "cancel": a["cancel"], "invites": a["invites"]}

    def event(self, eid: Any) -> dict[str, Any] | None:
        return next((e for e in self.w["events"] if e["id"] == str(eid)), None)

    def room(self, rid: Any) -> dict[str, Any] | None:
        return next((r for r in self.v["rooms"] if r["id"] == str(rid)), None)

    # judging after every change ----------------------------------------------------------------
    def check(self) -> None:
        s, w, a = self.s, self.w, self.ans
        reviews = [e for e in w["events"] if _cal_is_review(e)]
        q, b, vendor = self.event(a["ids"]["quarterly"]), self.event(a["ids"]["budget"]), self.event(a["ids"]["vendor"])
        met = {
            "common_slot": len(reviews) == 1 and sorted(reviews[0]["attendees"]) == ["ana", "ben", "chloe"]
            and (reviews[0]["day"], reviews[0]["start"], reviews[0]["duration"]) == (*a["slot"], 45),
            "book_room": q is not None and q["room"] == a["room"]
            and (q["day"], q["start"], q["duration"], sorted(q["attendees"])) == a["quarterly_at"],
            "fix_overlap": vendor is not None and b is not None and (b["day"], b["start"], b["duration"]) == a["budget_at"]
            and (vendor["day"], vendor["start"], vendor["duration"]) == (a["vendor_day"], a["vendor"], 30),
            "cancel_occurrence": w["series"] is not None and w["series"]["exdates"] == [a["cancel"]],
            "answer_invites": all(i["response"] == a["invites"][i["id"]] for i in w["invites"]),
        }
        for step, ok in met.items():
            if ok and step not in s.done and s.complete(step):
                s.say(*_CAL_DONE[step])

    def fields(self, data: dict[str, Any]) -> dict[str, Any] | str:
        title = squash(data.get("title"))[:80]
        if not title:
            return "Add a title."
        try:
            day = date.fromisoformat(norm(data.get("day")))
            start, duration = int(data.get("start")), int(data.get("duration"))
        except (TypeError, ValueError):
            return "Choose a day, a start time and a duration."
        if day.weekday() > 4 or start % 30 or not _CAL_FIRST <= start < _CAL_LAST or duration not in (30, 45, 60, 90):
            return "Choose a weekday, a start time and a duration from the lists."
        if start + duration > _CAL_LAST:
            return "The event must end by 18:00."
        known = {p["id"] for p in self.v["people"]}
        attendees: list[str] = []
        for pid in data.get("attendees") if isinstance(data.get("attendees"), list) else []:
            if str(pid) in known and str(pid) not in attendees:
                attendees.append(str(pid))
        room = norm(data.get("room"))
        if room and self.room(room) is None:
            return "No such room."
        return {"title": title, "day": day.isoformat(), "start": start, "duration": duration, "attendees": attendees, "room": room}

    def slot_problem(self, e: dict[str, Any]) -> str:
        v, start, end = self.v, e["start"], e["start"] + e["duration"]
        if e["day"] not in v["week"]:
            return "“Design review” belongs in this week."
        if (e["day"], start) < (v["today"], v["next_half"]):
            return "That time has already passed — the meeting can start at the next half hour at the earliest."
        firsts = {c["id"]: c["first"] for c in v["coworkers"]}
        for who, (a, b) in _CAL_HOURS.items():
            if not (a <= start and end <= b):
                return f"That is outside {firsts[who]}'s working hours."
        if not _cal_free(self.w, "me", e["day"], start, end, lambda i: i is e):
            return "You already have an event then."
        for who in _CAL_HOURS:
            if not _cal_free(self.w, who, e["day"], start, end):
                return f"{firsts[who]} is busy then."
        return "Everyone is free then, but there is an earlier time that works for all four of you."

    def feedback(self, e: dict[str, Any]) -> dict[str, Any]:
        """The card message after an event is created or changed."""
        s, a = self.s, self.ans
        if _cal_is_review(e):
            if sum(_cal_is_review(x) for x in self.w["events"]) > 1:
                return s.futility("slot", "There is more than one “Design review” — keep just one.")
            if sorted(e["attendees"]) != ["ana", "ben", "chloe"]:
                return s.futility("slot", "“Design review” needs exactly Ana, Ben and Chloé as attendees.")
            if e["duration"] != 45:
                return s.futility("slot", "“Design review” should last 45 minutes.")
            if (e["day"], e["start"]) != a["slot"]:
                return s.futility("slot", self.slot_problem(e))
            return {"ok": True}
        if e["id"] == a["ids"]["quarterly"]:
            if (e["day"], e["start"], e["duration"], sorted(e["attendees"])) != a["quarterly_at"]:
                return s.futility("room", "Keep “Quarterly planning” at its time with its attendees — it only needs a room.")
            room = self.room(e["room"])
            if room and room["id"] != a["room"]:  # a room booked at that time was refused before saving
                if "projector" not in room["equipment"]:
                    return s.futility("room", f"{room['name']} has no projector.")
                return s.futility("room", f"{room['name']} seats {room['seats']} — not enough for everyone invited plus you.")
            return {"ok": True}
        if e["id"] == a["ids"]["budget"] and (e["day"], e["start"], e["duration"]) != a["budget_at"]:
            return s.futility("overlap", "“Budget sync” is high priority — keep it where it was and move the low-priority event.")
        if e["id"] == a["ids"]["vendor"]:
            budget = self.event(a["ids"]["budget"])
            if e["day"] != a["vendor_day"]:
                return s.futility("overlap", "Keep “Vendor call” on the same day.")
            if e["duration"] != 30:
                return s.futility("overlap", "Keep “Vendor call” 30 minutes long.")
            if budget is None or (budget["day"], budget["start"], budget["duration"]) != a["budget_at"]:
                return s.futility("overlap", "“Budget sync” must stay where it was.")
            if e["start"] < budget["start"] + budget["duration"]:
                return s.futility("overlap", "“Vendor call” must start after “Budget sync” ends.")
            clash = next((i for x, y, i in _cal_mine(self.w, e["day"], lambda i: i is e) if _cal_overlap(e["start"], e["start"] + 30, x, y)), None)
            if clash:
                return s.futility("overlap", f"“Vendor call” now overlaps “{clash['title']}”.")
            if e["start"] != a["vendor"]:
                return s.futility("overlap", "There is an earlier free 30-minute slot after “Budget sync” ends.")
        return {"ok": True}

    def cancel_feedback(self, fresh: bool) -> dict[str, Any]:
        s, a, series = self.s, self.ans, self.w["series"]
        wrong = [d for d in series["exdates"] if d != a["cancel"]]
        if wrong:
            msg = (f"The 1:1 on {long_date(date.fromisoformat(wrong[-1]))} is cancelled, but only the one on "
                   f"{long_date(date.fromisoformat(a['cancel']))} should be. Undo brings it back.")
            return s.futility("cancel", msg) if fresh else s.say("cancel", msg)
        if "cancel_occurrence" not in s.done:
            return s.say("cancel", "")
        return {"ok": True}

    def act(self, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        s, w, a = self.s, self.w, self.ans
        s.log("page", kind, str(data.get("id") or data.get("title") or "")[:60])
        task_keys = {a["ids"]["quarterly"]: "room", a["ids"]["budget"]: "overlap", a["ids"]["vendor"]: "overlap"}
        if kind in ("create_event", "update_event"):
            target = self.event(data.get("id")) if kind == "update_event" else None
            if kind == "update_event" and target is None:
                return s.futility(None, "Only the organizer can change this event.")
            fields = self.fields(data)
            if isinstance(fields, str):
                return s.futility(None, fields)
            if fields["room"]:
                clash = _cal_room_clash(w, fields["room"], fields["day"], fields["start"], fields["start"] + fields["duration"],
                                        target["id"] if target else "")
                if clash:
                    when = f"{date.fromisoformat(fields['day']):%a} {_cal_hm(clash[0])}–{_cal_hm(clash[1])}"
                    return s.futility(task_keys.get(target["id"]) if target else None, f"{self.room(fields['room'])['name']} is already booked {when}.")
            if target is None:
                target = {"kind": "event", "id": f"e{self.next_id:02d}", "label": ""}
                self.next_id += 1
                w["events"].append(target)
            target.update(fields)
            s.touch()
            self.check()
            return dict(self.feedback(target), applied=True)
        if kind == "delete_event":
            eid, series = norm(data.get("id")), w["series"]
            if series and eid.startswith(series["id"] + ":"):
                day = eid.split(":", 1)[1]
                if not _cal_occurs(series, day):
                    return s.futility(None, "That event is not on the calendar.")
                if norm(data.get("scope")) == "all":
                    w["series"] = None
                    s.touch()
                    return s.fail("deleted the whole recurring 1:1 series")
                series["exdates"].append(day)
                self.trash.append({"title": series["title"], "day": day, "occurrence": day})
                s.touch()
                self.check()
                return dict(self.cancel_feedback(fresh=True), applied=True)
            e = self.event(eid)
            if e is None:
                return s.futility(None, "Change your response to an invitation under Invitations.")
            w["events"].remove(e)
            self.trash.append({"title": e["title"], "day": e["day"], "event": e})
            s.touch()
            self.check()
            if e["id"] in task_keys:
                return dict(s.futility(task_keys[e["id"]], f"“{e['title']}” was deleted — this task needs it. Undo brings it back."), applied=True)
            return {"ok": True, "applied": True}
        if kind == "undo":
            if not self.trash:
                return s.futility(None, "Nothing to undo.")
            item = self.trash.pop()
            if "event" in item:
                w["events"].append(item["event"])
                if item["event"]["id"] in task_keys:
                    s.say(task_keys[item["event"]["id"]], f"“{item['title']}” is back.")
            elif w["series"]:
                w["series"]["exdates"].remove(item["occurrence"])
            s.touch()
            self.check()
            if "occurrence" in item and w["series"]:
                self.cancel_feedback(fresh=False)
            return {"ok": True}
        if kind == "respond_invite":
            inv = next((i for i in w["invites"] if i["id"] == norm(data.get("id"))), None)
            response = norm(data.get("response"))
            if inv is None or response not in ("accept", "maybe", "decline"):
                return s.futility(None, "No such invitation.")
            inv["response"] = response
            s.touch()
            self.check()
            if "answer_invites" not in s.done and all(i["response"] for i in w["invites"]):
                wrong = sum(i["response"] != a["invites"][i["id"]] for i in w["invites"])
                return s.futility("invites", f"{wrong} of the 3 responses {'does' if wrong == 1 else 'do'} not follow the rules on the card.")
            return {"ok": True}
        return super().act(kind, data)


# =============================================================================================
# 8. CRM — accounts, contacts, a deal board and signed contracts; the records live in the harness
# =============================================================================================

_CRM_PAIRS = (  # (legal name, a look-alike company's legal name)
    ("Harbor Analytics LLC", "Harbour Analytica Ltd"),
    ("Northwind Logistics Inc.", "Northwinds Logistic Co."),
    ("Bluefin Robotics Corp.", "Blue Fin Robotic Corp."),
    ("Cedar Point Health LLC", "Cedarpoint Healthcare LLC"),
    ("Summit Ridge Foods Inc.", "Summit Ridge Food Co."),
    ("Ironclad Security Ltd", "Ironclad Securities Ltd"),
    ("Meridian Solar Inc.", "Meridian Solaris Inc."),
    ("Pinecrest Media LLC", "Pine Crest Media Group LLC"),
    ("Atlas Freightworks Inc.", "Atlas Freightway Ltd"),
    ("Copperleaf Software Ltd", "Copper Leaf Softworks Ltd"),
    ("Silverline Retail Inc.", "Silverlines Retail Inc."),
    ("Granite Bay Construction LLC", "Granitebay Builders LLC"),
)
_CRM_FILLERS = ("Brightwater Dental Group", "Kestrel Outdoor Co.", "Halcyon Print Works", "Oakridge Insurance LLC",
                "Redwood Freight Inc.", "Starling Hotels Ltd", "Tidewater Energy Corp.", "Vantage Legal LLP",
                "Willow Creek Farms", "Quarry Lane Architects", "Nimbus Cloud Systems", "Foxglove Pharmacy Inc.")
_CITIES = (("Portland", "OR", "97204"), ("Denver", "CO", "80202"), ("Austin", "TX", "78701"), ("Raleigh", "NC", "27601"),
           ("Columbus", "OH", "43215"), ("Madison", "WI", "53703"), ("Boise", "ID", "83702"), ("Tucson", "AZ", "85701"),
           ("Richmond", "VA", "23219"), ("Omaha", "NE", "68102"), ("Spokane", "WA", "99201"), ("Albany", "NY", "12207"),
           ("Savannah", "GA", "31401"), ("Tampa", "FL", "33602"))
_STREETS = ("Cedar Street", "Harbor Way", "Main Street", "Elm Avenue", "Commerce Drive", "Market Street", "Lakeview Road",
            "Industrial Parkway", "Oak Lane", "Broad Street")
_INDUSTRIES = ("Software", "Logistics", "Healthcare", "Manufacturing", "Financial services", "Retail", "Energy", "Media",
               "Food & beverage", "Construction")
_STAGES = ("Prospecting", "Qualification", "Proposal", "Negotiation", "Closed won")
_ACTIVITY_TYPES = ("Call", "Email", "Meeting")
_OUTCOMES = ("Connected", "Left voicemail", "No answer", "Meeting booked", "Not interested")
_NOTICE_DAYS = (30, 45, 60, 90, 120)
_CENTS = ("00", "15", "25", "40", "50", "65", "75", "80", "95")
_NICKNAMES = (("Jon", "Jonathan"), ("Liz", "Elizabeth"), ("Bill", "William"), ("Kate", "Katherine"), ("Mike", "Michael"),
              ("Sasha", "Alexandra"), ("Rob", "Robert"), ("Becca", "Rebecca"), ("Tom", "Thomas"), ("Nick", "Nicholas"))
_ACCENTED = (("Ramírez", "Ramirez"), ("Peña", "Pena"), ("Novák", "Novak"), ("Gómez", "Gomez"), ("Müller", "Muller"),
             ("Núñez", "Nunez"), ("Ibáñez", "Ibanez"), ("Dvořák", "Dvorak"))
_CALLEES = (("Dana Whitfield", "Dana Whitman"), ("Marcus Lee", "Marcus Leigh"), ("Ines Duarte", "Ines Durante"),
            ("Tomas Berg", "Tomas Bergh"), ("Priya Natarajan", "Priya Natarajah"), ("Owen Clarke", "Owen Clark"),
            ("Sofia Romano", "Sofia Romani"), ("Leila Haddad", "Leila Hadad"))
_CRM_FIRST = ("Alice", "Ben", "Carla", "Diego", "Elena", "Farid", "Grace", "Hugo", "Irene", "Jakob", "Keiko", "Luis", "Maya",
          "Nikolai", "Olga", "Pavel", "Rosa", "Samir", "Tara", "Victor", "Wen", "Yara")
_CRM_LAST = ("Abbott", "Brooks", "Castillo", "Dubois", "Engel", "Fischer", "Garcia", "Holm", "Jensen", "Kowalski", "Lund",
         "Moreno", "Nilsen", "Ortega", "Price", "Quinn", "Rossi", "Sato", "Trent", "Varga", "Walsh", "Young")
_CRM_TITLES = ("VP Operations", "Procurement Lead", "CFO", "IT Director", "Office Manager", "Head of Sales", "COO",
           "Finance Manager")
_DEAL_KINDS = ("new business", "expansion", "pilot", "renewal", "add-on seats")


def _short(legal: str) -> str:
    return re.sub(r"\s+(LLC|LLP|Inc\.|Ltd|Corp\.|Co\.)$", "", legal)


def _plain(text: Any) -> str:
    """A company or city name compared without case, extra spaces, periods or commas."""
    return squash(text).lower().replace(".", "").replace(",", "")


def add_months(day: date, months: int) -> date:
    index = day.year * 12 + day.month - 1 + months
    return date(index // 12, index % 12 + 1, day.day)  # callers keep day <= 28


def _iso(text: str) -> bool:
    """A real calendar date written YYYY-MM-DD."""
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", text))


class Crm(Gen6):
    name = "v6_crm"
    slug = "v6-crm"
    token = "BENCH_PASS v6-crm-2f84b6"
    family = "crm"
    steps = ("account_from_contract", "link_contact", "deal_stage", "log_call", "notice_period")

    def setup(self) -> None:
        today = app_today()
        next_step = add_business_days(today, 2)
        pairs = RNG.sample(_CRM_PAIRS, 9)
        (a_name, a_twin), (b_name, b_twin), (c_name, c_twin), (l_name, l_twin), (k_name, _) = pairs[:5]
        shadow = RNG.choice("abc")  # the look-alike contract shadows one of the three contract challenges

        def company(legal: str, place: tuple[str, str, str] | None = None, industry: str | None = None) -> dict[str, str]:
            city, state, zip_code = place or RNG.choice(_CITIES)
            short = _short(legal)
            return {"legal": legal, "short": short, "domain": re.sub(r"[^a-z]", "", short.lower()) + ".example",
                    "city": city, "state": state, "zip": zip_code, "industry": industry or RNG.choice(_INDUSTRIES),
                    "street": f"{RNG.randint(100, 9899)} {RNG.choice(_STREETS)}"}

        places, industries = RNG.sample(_CITIES, 4), RNG.sample(_INDUSTRIES, 4)
        keyed = (("a", a_name), ("b", b_name), ("c", c_name), ("twin", {"a": a_twin, "b": b_twin, "c": c_twin}[shadow]))
        cos = {key: company(name, places[i], industries[i]) for i, (key, name) in enumerate(keyed)}

        # --- contracts: distinct fees, notice periods and dates; no contract date equals a date answer
        notices = dict(zip(cos, RNG.sample(_NOTICE_DAYS, 4)))
        for _attempt in range(1000):
            terms: dict[str, dict[str, Any]] = {}
            for key in cos:
                term = RNG.choice((12, 24, 36))
                if key == "a":  # a new customer: the contract starts soon
                    start = today + timedelta(days=RNG.randint(3, 40))
                    start = start.replace(day=min(start.day, 28))
                    renewal = add_months(start, term)
                else:
                    renewal = today + timedelta(days=RNG.randint(45, 400))
                    renewal = renewal.replace(day=min(renewal.day, 28))
                    start = add_months(renewal, -term)
                fee = Decimal(f"{RNG.randint(1200, 9800)}.{RNG.choice(_CENTS)}")
                terms[key] = {"term": term, "start": start.isoformat(), "renewal": renewal.isoformat(), "fee": fee}
            close_answer = (date.fromisoformat(terms["b"]["renewal"]) - timedelta(days=30)).isoformat()
            dates = [t[k] for t in terms.values() for k in ("start", "renewal")]
            if (len(set(dates)) == len(dates) and close_answer not in dates and next_step.isoformat() not in dates
                    and len({t["fee"] for t in terms.values()}) == len(terms)):
                break
        else:  # pragma: no cover - a few draws always suffice
            raise RuntimeError("no distinct contract dates")
        documents = []
        for key, c in cos.items():
            notice = notices[key]
            documents.append({
                "key": key, "number": f"CT-{rand_digits(5)}", "customer": c["legal"],
                "address": f"{c['street']}, {c['city']}, {c['state']} {c['zip']}", "industry": c["industry"],
                "fee": str(terms[key]["fee"]), "start": terms[key]["start"], "term": terms[key]["term"],
                "renewal": terms[key]["renewal"], "notice_days": notice,
                "payment_days": RNG.choice([d for d in (15, 30, 45, 60) if d != notice]),  # page 1's decoy number of days
            })
        assert len({d["number"] for d in documents}) == len(documents)
        RNG.shuffle(documents)
        for i, d in enumerate(documents):
            d["id"] = f"DOC{i + 1}"
        doc = {d.pop("key"): d for d in documents}
        a_acv = terms["a"]["fee"] * 12

        # --- accounts: the B and C customers, the signature's company and its look-alike, the callee's company, fillers
        accounts: list[dict[str, Any]] = []

        def account(c: dict[str, str], acv: str, notice: str = "") -> dict[str, Any]:
            accounts.append({"id": "", "name": c["legal"], "city": c["city"], "industry": c["industry"],
                             "website": c["domain"], "acv": acv, "notice": notice})
            return accounts[-1]

        def some_acv() -> str:
            while True:
                value = f"{RNG.randint(24, 480) * 500}.00"
                if str(a_acv) not in value:
                    return value

        acc_b = account(cos["b"], str(terms["b"]["fee"] * 12), str(notices["b"]))
        acc_c = account(cos["c"], str(terms["c"]["fee"] * 12))
        l_co, lt_co, k_co = company(l_name), company(l_twin), company(k_name)
        acc_l = account(l_co, some_acv(), RNG.choice(("", "30", "60")))
        acc_lt = account(lt_co, some_acv(), RNG.choice(("", "45", "90")))
        acc_k = account(k_co, some_acv(), RNG.choice(("", "30", "60")))
        fillers = [account(company(n), some_acv(), RNG.choice(("", "", "30", "45", "60", "90")))
                   for n in [p[0] for p in pairs[5:]] + RNG.sample(_CRM_FILLERS, 7)]
        assert len({a["website"] for a in accounts} | {cos["a"]["domain"], cos["twin"]["domain"]}) == len(accounts) + 2
        assert all(_plain(a["name"]) not in (_plain(a_name), _plain(cos["twin"]["legal"])) for a in accounts)
        RNG.shuffle(accounts)
        for i, a in enumerate(accounts):
            a["id"] = f"AC{i + 1:02d}"

        # --- contacts: the signature's person exists unlinked; a namesake works at the look-alike company
        contacts: list[dict[str, Any]] = []

        def contact(name: str, email: str, title: str, acc: dict[str, Any] | None) -> dict[str, Any]:
            contacts.append({"id": "", "name": name, "email": email, "title": title,
                             "phone": f"({RNG.randint(201, 989)}) 555-{RNG.randint(1000, 9999)}",
                             "account_id": acc["id"] if acc else ""})
            return contacts[-1]

        def email_at(name: str, acc: dict[str, Any]) -> str:
            return name.lower().replace(" ", ".") + "@" + acc["website"]

        nick, formal = RNG.choice(_NICKNAMES)
        accented, plain_last = RNG.choice(_ACCENTED)
        person = f"{formal} {plain_last}"
        sig_email = f"{formal}.{plain_last}".lower() + "@" + l_co["domain"]
        target = contact(person, sig_email, "Partnerships Manager", None)
        contact(person, email_at(person, acc_lt), RNG.choice(_CRM_TITLES), acc_lt)
        callee, callee_twin = RNG.choice(_CALLEES)
        callee_contact = contact(callee, email_at(callee, acc_k), RNG.choice(_CRM_TITLES), acc_k)
        twin_home = RNG.choice(fillers)
        twin_contact = contact(callee_twin, email_at(callee_twin, twin_home), RNG.choice(_CRM_TITLES), twin_home)
        taken = {person, callee, callee_twin}
        names = [n for n in (f"{f} {l}" for f in RNG.sample(_CRM_FIRST, len(_CRM_FIRST)) for l in RNG.sample(_CRM_LAST, 2)) if n not in taken]
        homes = [acc_l, acc_l] + [a for a in accounts if a is not acc_lt for _ in range(RNG.choice((0, 1, 1, 2)))]
        for acc in homes[:22]:  # 13-26 contacts in all: the list spans two or three pages
            name = names.pop()
            contact(name, email_at(name, acc), RNG.choice(_CRM_TITLES), acc)
        assert len({c["email"] for c in contacts}) == len(contacts)
        assert sum(c["email"] == sig_email for c in contacts) == 1
        RNG.shuffle(contacts)
        for i, c in enumerate(contacts):
            c["id"] = f"C{i + 1:02d}"

        # --- deals: the B renewal (and a sibling) plus other pipeline; no deal closes on a date answer
        forbidden = {close_answer, next_step.isoformat()}

        def some_date() -> str:
            while True:
                day = (today + timedelta(days=RNG.randint(-30, 300))).isoformat()
                if day not in forbidden:
                    return day

        deals: list[dict[str, Any]] = []

        def deal(name: str, acc: dict[str, Any], stage: str, close: str, amount: str) -> dict[str, Any]:
            deals.append({"id": "", "name": name, "account_id": acc["id"], "stage": stage, "close": close, "amount": amount})
            return deals[-1]

        b_short = cos["b"]["short"]
        target_deal = deal(f"{b_short} renewal", acc_b, RNG.choice(_STAGES[:3]), terms["b"]["renewal"], acc_b["acv"])
        deal(f"{b_short} add-on seats", acc_b, RNG.choice(_STAGES), some_date(), some_acv())
        for acc in RNG.sample([a for a in accounts if a is not acc_b], 12):
            deal(f"{_short(acc['name'])} {RNG.choice(_DEAL_KINDS)}", acc, RNG.choice(_STAGES), some_date(), some_acv())
        assert len({d["name"] for d in deals}) == len(deals)
        RNG.shuffle(deals)
        for i, d in enumerate(deals):
            d["id"] = f"DL{i + 1:02d}"

        # --- activities: history only, all in the past
        activities: list[dict[str, Any]] = []

        def activity(c: dict[str, Any], kind: str, outcome: str, days_ago: int, note: str, next_ago: int | None) -> None:
            activities.append({"id": f"AT{len(activities) + 1:02d}", "contact_id": c["id"], "type": kind, "outcome": outcome,
                               "date": (today - timedelta(days=days_ago)).isoformat(), "note": note,
                               "next_step": (today - timedelta(days=next_ago)).isoformat() if next_ago is not None else ""})

        activity(callee_contact, "Call", "Connected", RNG.randint(20, 40), "Intro call about next year's plan.", RNG.randint(5, 15))
        activity(twin_contact, "Call", "Left voicemail", RNG.randint(2, 9), "Called about pricing.", None)
        others = [c for c in contacts if c not in (target, callee_contact, twin_contact)]
        for c, (kind, outcome, note) in zip(RNG.sample(others, 4), (
                ("Email", "", "Sent the proposal."), ("Meeting", "Meeting booked", "Quarterly review."),
                ("Call", "No answer", "Tried the main line."), ("Call", "Connected", "Discussed onboarding."))):
            activity(c, kind, outcome, RNG.randint(1, 60), note, RNG.choice((None, 1, 3)))

        self.v = {
            "today": today.isoformat(), "today_text": long_date(today),
            "accounts": accounts, "contacts": contacts, "deals": deals, "activities": activities, "documents": documents,
            "tasks": {
                "account_from_contract": {"company": cos["a"]["short"]},
                "link_contact": {"signature": f"{nick} {accented} · Partnerships · {l_co['short']} · {sig_email}"},
                "deal_stage": {"deal": target_deal["name"], "company": cos["b"]["legal"]},
                "log_call": {"contact": callee, "company": k_co["short"]},
                "notice_period": {"account": acc_c["name"]},
            },
            "a": {"contract_id": doc["a"]["id"], "legal": cos["a"]["legal"], "city": cos["a"]["city"],
                  "state": cos["a"]["state"], "industry": cos["a"]["industry"], "acv": str(a_acv)},
            "link": {"account_id": acc_l["id"], "contact_id": target["id"], "email": sig_email,
                     "before": sorted(c["id"] for c in contacts if c["account_id"] == acc_l["id"])},
            "deal": {"id": target_deal["id"], "contract_id": doc["b"]["id"], "close": close_answer,
                     "before": {d["id"]: [d["stage"], d["close"]] for d in deals}},
            "call": {"contact_id": callee_contact["id"], "next_step": next_step.isoformat()},
            "notice": {"account_id": acc_c["id"], "contract_id": doc["c"]["id"], "days": notices["c"],
                       "before": {a["id"]: a["notice"] for a in accounts}},
        }
        blob = json.dumps(self.public())
        if any(answer in blob for answer in (close_answer, next_step.isoformat(), str(a_acv))):
            self.setup()  # a chance collision put an answer in the page data: draw everything again

    def public(self) -> dict[str, Any]:
        v = self.v
        public = {k: v[k] for k in ("today", "today_text", "accounts", "contacts", "deals", "activities", "documents", "tasks")}
        public.update(industries=_INDUSTRIES, stages=_STAGES, types=_ACTIVITY_TYPES, outcomes=_OUTCOMES)
        return public

    def truth(self) -> dict[str, Any]:
        return {k: self.v[k] for k in ("today", "a", "link", "deal", "call", "notice")}

    def find(self, kind: str, rid: Any) -> dict[str, Any] | None:
        return next((r for r in self.v[kind] if r["id"] == str(rid or "")), None)

    def account_problems(self, acc: dict[str, Any]) -> list[str]:
        a = self.v["a"]
        return [label for label, ok in (
            ("legal name", _plain(acc["name"]) == _plain(a["legal"])),
            ("billing city", _plain(acc["city"]) in (_plain(a["city"]), _plain(f"{a['city']}, {a['state']}"))),
            ("industry", acc["industry"] == a["industry"]),
            ("annual contract value", money(acc["acv"]) == Decimal(a["acv"])),
        ) if not ok]

    # judging after every change ----------------------------------------------------------------
    def check(self) -> None:
        s, v = self.s, self.v
        if "account_from_contract" not in s.done and any(a.get("created") and not self.account_problems(a) for a in v["accounts"]):
            s.complete("account_from_contract")
            s.say("account", "Account created from the contract ✓")
        link = v["link"]
        linked = {c["id"] for c in v["contacts"] if c["account_id"] == link["account_id"]}
        if "link_contact" not in s.done and linked == set(link["before"]) | {link["contact_id"]}:
            s.complete("link_contact")
            s.say("link", "Contact linked to the account ✓")
        target = self.find("deals", v["deal"]["id"])
        others_kept = all([d["stage"], d["close"]] == v["deal"]["before"][d["id"]] for d in v["deals"] if d is not target)
        if "deal_stage" not in s.done and target["stage"] == "Negotiation" and target["close"] == v["deal"]["close"] and others_kept:
            s.complete("deal_stage")
            s.say("deal", "Renewal deal updated ✓")
        notice, accounts = v["notice"], {a["id"]: a for a in v["accounts"]}
        if ("notice_period" not in s.done and accounts[notice["account_id"]]["notice"] == str(notice["days"])
                and all(accounts[aid]["notice"] == was for aid, was in notice["before"].items() if aid != notice["account_id"])):
            s.complete("notice_period")
            s.say("notice", "Notice period filled in ✓")

    def account_fields(self, data: dict[str, Any]) -> dict[str, str] | str:
        raw = data.get("fields") if isinstance(data.get("fields"), dict) else {}
        f = {k: squash(raw.get(k))[:200] for k in ("name", "city", "industry", "website", "acv", "notice")}
        if not f["name"]:
            return "An account needs a legal name."
        if f["industry"] and f["industry"] not in _INDUSTRIES:
            return "Choose an industry from the list."
        if f["acv"]:
            value = money(f["acv"])
            if value is None:
                return "Annual contract value must be an amount like 12500.00."
            f["acv"] = str(value)
        if f["notice"] and not f["notice"].isdigit():
            return "Notice period must be a whole number of days."
        return f

    def contact_fields(self, data: dict[str, Any], rid: str = "") -> dict[str, str] | str:
        raw = data.get("fields") if isinstance(data.get("fields"), dict) else {}
        f = {k: squash(raw.get(k))[:200] for k in ("name", "title", "email", "phone", "account_id")}
        if not f["name"] or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", f["email"]):
            return "A contact needs a name and a valid email address."
        if f["account_id"] and not self.find("accounts", f["account_id"]):
            return "No such account."
        if any(c["email"].lower() == f["email"].lower() and c["id"] != rid for c in self.v["contacts"]):
            return "A contact with this email already exists — link it instead."
        return f

    def link_feedback(self, c: dict[str, Any], before: str) -> dict[str, Any]:
        """Feedback on a contact whose account may have changed (it was ``before``)."""
        link, saved = self.v["link"], {"saved": True, "id": c["id"]}
        if c["account_id"] != before and c["account_id"] == link["account_id"] and c["id"] != link["contact_id"]:
            return {**self.s.futility("link", f"{c['name']} ({c['email']}) is not the person in the signature."), **saved}
        if c["account_id"] != before and c["id"] == link["contact_id"] and c["account_id"] not in ("", link["account_id"]):
            return {**self.s.futility("link", "That account is not the company in the signature."), **saved}
        return {"ok": True, **saved}

    def act(self, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        s, v = self.s, self.v
        s.log("page", kind, str(data.get("id") or data.get("contact_id") or "")[:60])
        if kind in ("create_account", "update_account"):
            acc = self.find("accounts", data.get("id")) if kind == "update_account" else None
            if kind == "update_account" and acc is None:
                return s.futility(None, "No such account.")
            fields = self.account_fields(data)
            if isinstance(fields, str):
                return s.futility(None, fields)
            if any(_plain(a["name"]) == _plain(fields["name"]) and a is not acc for a in v["accounts"]):
                return s.futility("account", "An account with this name already exists — open it and edit it instead.")
            if acc is None:
                acc = {"id": f"AC{len(v['accounts']) + 1:02d}", "created": True}
                v["accounts"].append(acc)
            notice_before = acc.get("notice", "")
            acc.update(fields)
            s.touch()
            self.check()
            done = {"saved": True, "id": acc["id"]}
            if acc.get("created"):
                wrong = self.account_problems(acc)
                if wrong:
                    msg = "Account saved, but these fields don't match the contract: " + ", ".join(wrong) + "."
                    return {**s.futility("account", msg), **done}
            elif acc["id"] == v["notice"]["account_id"]:
                if acc["notice"] and acc["notice"] != str(v["notice"]["days"]):
                    return {**s.futility("notice", f"That is not the notice period in {acc['name']}'s contract."), **done}
            elif acc["notice"] != notice_before:
                was = v["notice"]["before"][acc["id"]] or "empty"
                return {**s.futility("notice", f"{acc['name']} is not the account in the task — its notice period was {was}."), **done}
            return {"ok": True, **done}
        if kind in ("create_contact", "update_contact"):
            c = self.find("contacts", data.get("id")) if kind == "update_contact" else None
            if kind == "update_contact" and c is None:
                return s.futility(None, "No such contact.")
            fields = self.contact_fields(data, c["id"] if c else "")
            if isinstance(fields, str):
                return s.futility("link" if "already exists" in fields else None, fields)
            if c is None:
                c = {"id": f"C{len(v['contacts']) + 1:02d}", "account_id": ""}
                v["contacts"].append(c)
            before = c["account_id"] if kind == "update_contact" else ""
            c.update(fields)
            s.touch()
            self.check()
            if kind == "create_contact" and c["account_id"] == v["link"]["account_id"]:
                msg = f"{c['name']} was added to that account, but {c['email']} is not the signature's email."
                return {**s.futility("link", msg), "saved": True, "id": c["id"]}
            return self.link_feedback(c, before)
        if kind in ("link_contact", "unlink_contact"):
            c = self.find("contacts", data.get("contact_id"))
            account_id = str(data.get("account_id") or "") if kind == "link_contact" else ""
            if c is None or (kind == "link_contact" and not self.find("accounts", account_id)):
                return s.futility(None, "No such contact or account.")
            before = c["account_id"]
            c["account_id"] = account_id
            s.touch()
            self.check()
            return self.link_feedback(c, before)
        if kind == "update_deal":
            d = self.find("deals", data.get("id"))
            if d is None:
                return s.futility(None, "No such deal.")
            stage = norm(data.get("stage")) or d["stage"]
            close = norm(data.get("close")) or d["close"]
            if stage not in _STAGES:
                return s.futility(None, "No such stage.")
            if not _iso(close):
                return s.futility(None, "Use a close date like 2027-05-31 (YYYY-MM-DD).")
            moved, redated = stage != d["stage"], close != d["close"]
            d["stage"], d["close"] = stage, close
            s.touch()
            self.check()
            if d["id"] != v["deal"]["id"]:
                was = v["deal"]["before"][d["id"]]
                if [d["stage"], d["close"]] != was:
                    return s.futility("deal", f"“{d['name']}” is not the deal in the task — it was in {was[0]}, closing {was[1]}.")
            elif moved and stage != "Negotiation":
                return s.futility("deal", "The renewal deal is not in the stage the task asks for.")
            elif redated and close != v["deal"]["close"]:
                return s.futility("deal", "That close date is not 30 days before the renewal date in the contract.")
            return {"ok": True}
        if kind == "log_activity":
            c = self.find("contacts", data.get("contact_id"))
            kind_, outcome, next_day = norm(data.get("type")), norm(data.get("outcome")), norm(data.get("next_step"))
            note = norm(data.get("note"))[:1000]
            if c is None:
                return s.futility(None, "No such contact.")
            if kind_ not in _ACTIVITY_TYPES or (outcome and outcome not in _OUTCOMES):
                return s.futility(None, "Choose an activity type and an outcome from the lists.")
            if next_day and not _iso(next_day):
                return s.futility(None, "Use a next-step date like 2027-05-31 (YYYY-MM-DD).")
            v["activities"].append({"id": f"AT{len(v['activities']) + 1:02d}", "contact_id": c["id"], "type": kind_,
                                    "outcome": outcome, "date": v["today"], "note": note, "next_step": next_day})
            s.touch()
            call = v["call"]
            if c["id"] != call["contact_id"]:
                return {**s.futility("call", f"That activity was logged on {c['name']}, not the contact you called."), "saved": True}
            wrong = [label for label, ok in (
                ("type", kind_ == "Call"), ("outcome", outcome == "Left voicemail"),
                ("next-step date", next_day == call["next_step"]), ("note", "pricing" in note.lower()),
            ) if not ok]
            if wrong:
                return {**s.futility("call", "Activity saved, but these are not right: " + ", ".join(wrong) + "."), "saved": True}
            s.complete("log_call")
            return {**s.say("call", "Call logged ✓"), "saved": True}
        return super().act(kind, data)


# =============================================================================================
# 9. Docs — a document editor with review tools; each document lives in the page until it is saved
# =============================================================================================

_REVIEWERS = (("M. Okafor", "N. Okafor"), ("J. Lind", "J. Lindh"), ("R. Castillo", "R. Castilla"),
              ("A. Novak", "A. Nowak"), ("S. Brennan", "S. Brennen"), ("T. Ahmadi", "T. Ahmady"))
_BOTS = ("AutoFormat", "StyleBot", "TidyWriter", "LintPilot")
_SUGGESTED = (  # (before, kind, suggested text, after): each sentence reads correctly with or without the change
    ("Invoices are payable within 30 days", "ins", " of receipt", "."),
    ("The Vendor shall", "del", " promptly", " notify the Client of any delay."),
    ("Either party may end this Agreement with", "ins", " at least", " 60 days' written notice."),
    ("All prices are in US dollars", "del", " and include applicable taxes", "."),
    ("The Client will provide", "ins", " reasonable", " access to its premises."),
    ("Warranty claims must be raised", "ins", " in writing", " within twelve months."),
    ("Each party keeps the other's information", "del", " strictly", " confidential."),
    ("This Agreement is governed by the laws of", "del", " the State of", " New York."),
    ("Disputes go first to", "ins", " the", " account managers of both parties."),
    ("The Vendor carries", "ins", " commercial", " liability insurance of $2 million."),
    ("Late deliveries", "del", " may", " incur a penalty of 2% per week."),
    ("Goods are inspected", "ins", " by the Client", " on arrival."),
)
_VENDOR_HEADINGS = ("1. Scope", "2. Commercial terms", "3. General")
_BRIEF = (  # (section, ((sentence, the phrase a comment thread can anchor to), ...))
    ("Overview", (("The new mobile app launches to all customers in spring.", "launches to all customers"),
                  ("Success means 5,000 weekly active users by June.", "5,000 weekly active users"))),
    ("Audience", (("Our first audience is small retail teams.", "small retail teams"),
                  ("Beta testers come from the top 50 accounts.", "top 50 accounts"))),
    ("Plan", (("Marketing spend is capped at $40,000.", "capped at $40,000"),
              ("Support will staff a launch-week help desk.", "launch-week help desk"),
              ("Pricing stays the same for existing plans.", "existing plans"),
              ("The press release goes out the day before launch.", "press release"))),
    ("Risks", (("App store review could delay the release.", "App store review"),)),
)
_OPENERS = ("Can we firm up this date?", "Is this number still current?", "Should legal review this?",
            "This reads a bit vague.", "Who owns this item?", "Do we have budget for this?",
            "Can we cite the survey here?", "Is this list final?", "Let's add the pricing tiers.")
_REPLIES = ("Good point, updating now.", "Agreed.", "I'll check with finance.", "Yes, confirmed today.",
            "Let's discuss at standup.", "Changed it to match the deck.", "Not yet, waiting on design.",
            "Fine by me.", "Done, please take a look.")
_CONTRACT = (  # {w}: one form of the word — the target "Contractor" or a look-alike
    "The {w} must keep accurate time records for every engagement.",
    "Invoices from the {w} are due monthly in arrears.",
    "The {w} may not assign this contract without written consent.",
    "Equipment supplied by the {w} is not transferred to the Client.",
    "The Client will give the {w} access to its systems as needed.",
    "Any delay caused by the {w} must be reported within two days.",
    "The {w} carries professional indemnity insurance.",
    "Work done by the {w} is subject to acceptance testing.",
    "The {w} will follow the Client's security policies.",
    "Staff of the {w} report to their own managers.",
)
_CONTRACTOR = re.compile(r"(?<![A-Za-z0-9_])Contractor(?![A-Za-z0-9_])")
_POLICIES = (
    ("Working Hours", "Core hours are 10:00 to 16:00 on weekdays.", "Overtime needs a manager's approval."),
    ("Remote Work", "Staff may work remotely up to three days a week.", "Home offices must pass the safety checklist."),
    ("Annual Leave", "Full-time staff receive 25 days of leave a year.", "Leave requests need two weeks' notice."),
    ("Expenses", "Claims must be filed within 30 days.", "Receipts are required for items over $25."),
    ("Travel", "Book economy class for flights under six hours.", "Use the travel portal for all bookings."),
    ("IT Equipment", "Laptops are replaced every three years.", "Report lost devices to IT within one hour."),
    ("Data Security", "Lock your screen when you leave your desk.", "Never share passwords, even with IT."),
    ("Code of Conduct", "Treat colleagues and customers with respect.", "Report concerns to HR or the ethics line."),
    ("Payment Terms", "Salaries are paid on the last business day of the month.", "Payslips are in the HR portal."),
    ("Termination", "Notice periods are set in each employment contract.", "Return company property on your last day."),
)
_SLA_TOPICS = (  # (topic, the one sentence about it, near-miss sentences that share its words)
    ("service credits", "Service credits are paid as a share of the monthly fee.",
     ("Service levels are reviewed with the customer every quarter.", "Credit notes are issued within ten business days.")),
    ("planned maintenance", "Planned maintenance is announced at least five days ahead.",
     ("Maintenance of customer hardware is not covered.", "Planned releases are listed in the change calendar.")),
    ("response times", "Response times for priority 1 incidents are capped at 30 minutes.",
     ("Resolution targets depend on the incident priority.", "Response templates are kept in the support portal.")),
    ("data backups", "Data backups run nightly and are kept for 35 days.",
     ("Data exports are available on request.", "Backup power is provided by the data centre.")),
)
_SLA_FILLER = ("Uptime is measured monthly across all regions.", "Support is available 24 hours a day.",
               "The customer sets the priority when raising a ticket.", "Reports are sent on the first business day.",
               "Either party may ask for a review of these terms.", "Escalations go to the named account manager.")
_NEW_NOTES = ("See Schedule B.", "As defined in clause 7.2.", "Per the 2027 rate card.", "Subject to the annual review.")
_OLD_NOTES = ("Measured in UTC.", "Business days only.", "Excludes customer-caused outages.")


def split_sentences(text: Any) -> list[str]:
    """The editor's sentence boundaries; the page splits paragraphs the same way."""
    return re.split(r"(?<=[.!?])\s+", squash(text))


def _block(blocks: list[dict[str, Any]], kind: str, **fields: Any) -> str:
    bid = f"b{len(blocks) + 1}"
    blocks.append({"id": bid, "kind": kind, **fields})
    return bid


def _merge(segments: list[dict[str, str]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for seg in segments:
        if "text" in seg and out and "text" in out[-1]:
            out[-1] = {"text": out[-1]["text"] + seg["text"]}
        else:
            out.append(dict(seg))
    return out


def _resolved_text(segments: list[Any], suggestions: dict[str, dict[str, Any]], status: dict[str, str] | None = None) -> str:
    """A paragraph's text with its suggestions applied; one still open stays in place as a ⟦id⟧ marker."""
    out = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        sid = norm(seg.get("sug"))
        if not sid:
            out.append(str(seg.get("text") or ""))
            continue
        state = (status or {}).get(sid, "open")
        if state == "open":
            out.append(f"⟦{sid}⟧")
        elif (state == "accepted") == (suggestions[sid]["type"] == "ins"):
            out.append(suggestions[sid]["text"])
    return squash("".join(out))


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


class Docs(Gen6):
    name = "v6_docs"
    slug = "v6-docs"
    token = "BENCH_PASS v6-docs-a58f02"
    family = "document_editing"
    steps = ("review_suggestions", "resolve_threads", "scoped_replace", "outline_reorder", "footnote")
    msg_keys = {"review_suggestions": "sug", "resolve_threads": "threads", "scoped_replace": "replace",
                "outline_reorder": "outline", "footnote": "footnote"}
    praise = {"review_suggestions": "Suggestions reviewed ✓", "resolve_threads": "Threads resolved ✓",
              "scoped_replace": "Replacements are right ✓", "outline_reorder": "Outline reordered ✓",
              "footnote": "Footnote added ✓"}

    def setup(self) -> None:
        people = [name for name, _ in _COLLEAGUES]
        RNG.shuffle(people)
        self.docs: dict[str, dict[str, Any]] = {}
        self.v = {"tasks": {}}
        self.setup_review(people[0])
        self.setup_threads(people[1], people[2:6])
        self.setup_contract(people[6])
        self.setup_handbook(people[7])
        self.setup_sla(people[8])

    def add_doc(self, step: str, title: str, owner: str, blocks: list[dict[str, Any]], **extra: Any) -> None:
        self.docs[step] = {"id": step, "title": title, "owner": owner, "blocks": blocks,
                           "suggestions": extra.get("suggestions", []), "threads": extra.get("threads", []),
                           "footnotes": extra.get("footnotes", [])}

    # --- 1. tracked suggestions from four authors: accept one, reject the bot, leave the rest open
    def setup_review(self, owner: str) -> None:
        first, second = RNG.sample(_REVIEWERS, 2)
        accept, twin = RNG.sample(first, 2)  # either spelling may be the real reviewer
        other, bot = RNG.choice(second), RNG.choice(_BOTS)
        n_bot = RNG.choice((2, 3))
        authors = [accept] * 3 + [bot] * n_bot + [twin] + [other] * (4 - n_bot)
        RNG.shuffle(authors)
        picked = RNG.sample(_SUGGESTED, len(authors))
        sizes = [2, 2, 2, 1, 1]
        RNG.shuffle(sizes)
        blocks: list[dict[str, Any]] = []
        suggestions: list[dict[str, Any]] = []
        for i, size in enumerate(sizes):
            if i % 2 == 0:
                _block(blocks, "h", level=2, text=_VENDOR_HEADINGS[i // 2])
            bid = _block(blocks, "p", segments=[])
            segments: list[dict[str, str]] = []
            for _ in range(size):
                k = len(suggestions)
                before, kind, text, after = picked[k]
                segments += [{"text": (" " if segments else "") + before}, {"sug": f"s{k + 1}"}, {"text": after}]
                suggestions.append({"id": f"s{k + 1}", "type": kind, "text": text, "author": authors[k],
                                    "bot": authors[k] == bot, "block": bid, "status": "open"})
            blocks[-1]["segments"] = _merge(segments)
        status = {s["id"]: "accepted" if s["author"] == accept else "rejected" if s["author"] == bot else "open"
                  for s in suggestions}
        assert list(status.values()).count("accepted") == 3 and list(status.values()).count("open") >= 2
        by_id = {s["id"]: s for s in suggestions}
        text = {b["id"]: _resolved_text(b["segments"], by_id, status) if b["kind"] == "p" else b["text"] for b in blocks}
        self.add_doc("review_suggestions", "Vendor agreement", owner, blocks, suggestions=suggestions)
        self.v["review"] = {"status": status, "text": text}
        self.v["tasks"]["review_suggestions"] = {"accept": accept, "reject": bot}

    # --- 2. comment threads: resolve the ones the owner replied to
    def setup_threads(self, owner: str, others: list[str]) -> None:
        twin = _LOOKALIKES[owner]
        plans = {  # kind -> authors in order (the first comment opens the thread; the rest are replies)
            "replied": [others[0], owner],
            "replied_late": [others[1], others[2], owner, others[1]],
            "followed_up": [owner, others[3], owner],
            "alone": [owner],
            "others_only": [owner, others[2], others[0]],
            "twin": [others[3], twin],
            "long_no_owner": [others[0], others[1], others[3], others[2]],
        }
        kinds = list(plans)
        RNG.shuffle(kinds)
        blocks: list[dict[str, Any]] = []
        lines: list[tuple[str, str]] = []  # (block id, anchor phrase)
        for section, sentences in _BRIEF:
            _block(blocks, "h", level=2, text=section)
            for sentence, quote in sentences:
                lines.append((_block(blocks, "p", text=sentence), quote))
        threads = []
        for n, line in enumerate(sorted(RNG.sample(range(len(lines)), len(kinds)))):
            kind = kinds[n]
            replies = RNG.sample(_REPLIES, len(plans[kind]) - 1)
            if kind == "long_no_owner":  # someone else speaks for the owner, behind the collapsed replies
                replies[RNG.randint(1, len(replies) - 1)] = f"{owner.split()[0]} signed off on this on Monday."
            texts = [RNG.choice(_OPENERS)] + replies
            threads.append({"id": f"t{n + 1}", "block": lines[line][0], "quote": lines[line][1], "resolved": False,
                            "comments": [{"author": a, "text": t} for a, t in zip(plans[kind], texts)]})
        resolved = [t["id"] for t in threads if any(c["author"] == owner for c in t["comments"][1:])]
        assert len(resolved) == 3 and len(threads) == 7
        self.add_doc("resolve_threads", "Launch brief", owner, blocks, threads=threads)
        self.v["threads"] = {"owner": owner, "resolved": resolved}
        self.v["tasks"]["resolve_threads"] = {"owner": owner}

    # --- 3. a case-sensitive whole-word replace in body paragraphs only
    def setup_contract(self, owner: str) -> None:
        forms = ["Contractor"] * 4 + ["Contractors", "Subcontractor", "contractor"]
        forms += [RNG.choice(("Contractor", "Contractor", "Contractors", "Subcontractor", "contractor")) for _ in range(2)]
        RNG.shuffle(forms)
        sentences = [line.format(w=form) for line, form in zip(RNG.sample(_CONTRACT, len(forms)), forms)]
        sizes = [2, 1, 2, 1, 2, 1]
        RNG.shuffle(sizes)
        titles = [("2. Contractor duties", "2. Duties"), ("4. Contractor staff", "4. Staffing")]
        named = RNG.randrange(2)  # at least one heading holds the word
        headings = [pair[0] if i == named or RNG.random() < 0.5 else pair[1] for i, pair in enumerate(titles)]
        rows = [["Contractor", "The party that performs the Services."], ["Subcontractor", "A third party the Contractor engages."],
                ["Client", "The party that receives the Services."]]
        RNG.shuffle(rows)
        blocks: list[dict[str, Any]] = []
        _block(blocks, "p", text="This contract sets out how the Services are provided.")
        _block(blocks, "h", level=2, text="1. Definitions")
        _block(blocks, "table", head=["Term", "Meaning"], rows=rows)
        for i, size in enumerate(sizes):
            if i % 2 == 0:
                _block(blocks, "h", level=2, text=(headings[0], "3. Fees and invoicing", headings[1])[i // 2])
            _block(blocks, "p", text=" ".join(sentences[:size]))
            sentences = sentences[size:]
        paragraphs = [b for b in blocks if b["kind"] == "p"]
        count = sum(len(_CONTRACTOR.findall(b["text"])) for b in paragraphs)
        assert count >= 4 and any(_CONTRACTOR.search(b["text"]) for b in blocks if b["kind"] == "h")
        assert not sentences and any(_CONTRACTOR.search(cell) for row in rows for cell in row)
        self.add_doc("scoped_replace", "Services contract", owner, blocks)
        self.v["replace"] = {"text": {b["id"]: _CONTRACTOR.sub("Supplier", b["text"]) for b in paragraphs}, "count": count}

    # --- 4. move one section above another and fix its heading level
    def setup_handbook(self, owner: str) -> None:
        sections = RNG.sample(_POLICIES, 6)
        # the moved section (i) needs at least two single-step moves to land directly above the other (j)
        i, j = RNG.choice([(i, j) for i in range(6) for j in range(6) if j >= i + 3 or i >= j + 2])
        blocks: list[dict[str, Any]] = []
        chunks = [[_block(blocks, "p", text="This handbook applies to every employee.")]]  # then one per section
        for k, (title, first, second) in enumerate(sections):
            chunk = [_block(blocks, "h", level=RNG.choice((1, 3)) if k == i else 2, text=title), _block(blocks, "p", text=first)]
            if k == i or RNG.random() < 0.5:  # the moved section always has two paragraphs
                chunk.append(_block(blocks, "p", text=second))
            chunks.append(chunk)
        heads = [chunk[0] for chunk in chunks[1:]]
        moving = chunks.pop(1 + i)
        chunks.insert(next(n for n, chunk in enumerate(chunks) if chunk[0] == heads[j]), moving)
        order = [bid for chunk in chunks for bid in chunk]
        assert order != [b["id"] for b in blocks] and order.index(heads[i]) < order.index(heads[j])
        self.add_doc("outline_reorder", "Policy handbook", owner, blocks)
        self.v["outline"] = {"order": order, "levels": {head: 2 for head in heads}, "heading": heads[i],
                             "moves": j - i - 1 if i < j else i - j}
        self.v["tasks"]["outline_reorder"] = {"section": sections[i][0], "above": sections[j][0]}

    # --- 5. a footnote on the one sentence about a topic
    def setup_sla(self, owner: str) -> None:
        topic, target, decoys = RNG.choice(_SLA_TOPICS)
        pool = [s for t in _SLA_TOPICS if t[0] != topic for s in (t[1], *t[2])] + list(_SLA_FILLER)
        sentences = [target, *decoys, *RNG.sample(pool, 9)]
        RNG.shuffle(sentences)
        assert sum(topic in s.lower() for s in sentences) == 1
        blocks: list[dict[str, Any]] = []
        anchors = []  # (block id, sentence index) for every sentence, in reading order
        for n, heading in enumerate(("Availability", "Support", "Billing")):
            _block(blocks, "h", level=2, text=heading)
            for pair in (sentences[4 * n:4 * n + 2], sentences[4 * n + 2:4 * n + 4]):
                bid = _block(blocks, "p", text=" ".join(pair))
                assert split_sentences(blocks[-1]["text"]) == pair
                anchors += [(bid, 0), (bid, 1)]
        spot = anchors[sentences.index(target)]
        old = RNG.choice([a for a in anchors if a != spot])
        notes = [{"id": "f1", "block": old[0], "sentence": old[1], "text": RNG.choice(_OLD_NOTES)}]
        text = RNG.choice(_NEW_NOTES)
        self.add_doc("footnote", "SLA summary", owner, blocks, footnotes=notes)
        self.v["footnote"] = {"block": spot[0], "sentence": spot[1], "sentence_text": target, "text": text}
        self.v["tasks"]["footnote"] = {"topic": topic, "text": text}

    def public(self) -> dict[str, Any]:
        return {"docs": [self.docs[step] for step in self.steps], "tasks": self.v["tasks"]}

    # --- judging a saved document -----------------------------------------------------------------
    def judge_review_suggestions(self, data: dict[str, Any], blocks: dict[str, dict[str, Any]]) -> str | None:
        v = self.v["review"]
        got = {norm(s.get("id")): norm(s.get("status")) for s in data.get("suggestions") or [] if isinstance(s, dict)}
        wrong = sum(1 for sid, want in v["status"].items() if got.get(sid) != want)
        if wrong:
            return f"{wrong} suggestion{'s are' if wrong > 1 else ' is'} in the wrong state."
        by_id = {s["id"]: s for s in self.docs["review_suggestions"]["suggestions"]}
        for bid, want in v["text"].items():
            b = blocks.get(bid) or {}
            segments = b.get("segments")
            text = _resolved_text(segments, by_id) if isinstance(segments, list) else squash(b.get("text"))
            if text != want:
                return "The text no longer matches those decisions — something else was edited. Restore the original and review again."
        return None

    def judge_resolve_threads(self, data: dict[str, Any], blocks: dict[str, dict[str, Any]]) -> str | None:
        known = {t["id"] for t in self.docs["resolve_threads"]["threads"]}
        got = {norm(t.get("id")) for t in data.get("threads") or [] if isinstance(t, dict) and t.get("resolved") is True}
        wrong = len((got & known) ^ set(self.v["threads"]["resolved"]))
        if wrong:
            return f"{wrong} thread{'s are' if wrong > 1 else ' is'} in the wrong state (resolved vs open)."
        return None

    def judge_scoped_replace(self, data: dict[str, Any], blocks: dict[str, dict[str, Any]]) -> str | None:
        original = self.docs["scoped_replace"]["blocks"]
        if any(b["id"] not in blocks for b in original):
            return "A block is missing — restore the original and try again."
        if any(squash(blocks[b["id"]].get("text")) != b["text"] for b in original if b["kind"] == "h"):
            return "A heading was changed — headings keep their wording."
        for b in (b for b in original if b["kind"] == "table"):
            rows = blocks[b["id"]].get("rows")
            if not isinstance(rows, list) or [[squash(c) for c in r] for r in rows if isinstance(r, list)] != b["rows"]:
                return "The Definitions table was changed — it keeps its wording."
        bad = sum(1 for bid, want in self.v["replace"]["text"].items() if squash(blocks[bid].get("text")) != want)
        if bad:
            return f"{bad} paragraph{'s do' if bad > 1 else ' does'} not read as expected."
        return None

    def judge_outline_reorder(self, data: dict[str, Any], blocks: dict[str, dict[str, Any]]) -> str | None:
        v, task = self.v["outline"], self.v["tasks"]["outline_reorder"]
        order = [norm(b.get("id")) for b in data.get("blocks") or [] if isinstance(b, dict)]
        if sorted(order) != sorted(v["order"]):
            return "Blocks are missing or duplicated — restore the original and try again."
        if order != v["order"]:
            if [i for i in order if i in v["levels"]] != [i for i in v["order"] if i in v["levels"]]:
                return "The sections are not in the requested order."
            return "A paragraph is no longer under its own heading."
        if _int(blocks[v["heading"]].get("level")) != 2:
            return f"The “{task['section']}” heading is not level 2."
        if any(_int(blocks[hid].get("level")) != level for hid, level in v["levels"].items()):
            return "Another heading's level was changed."
        return None

    def judge_footnote(self, data: dict[str, Any], blocks: dict[str, dict[str, Any]]) -> str | None:
        v, task = self.v["footnote"], self.v["tasks"]["footnote"]
        notes = [n for n in data.get("footnotes") or [] if isinstance(n, dict)]
        got = {norm(n.get("id")): n for n in notes}
        for old in self.docs["footnote"]["footnotes"]:
            mine = got.get(old["id"])
            if not mine or (norm(mine.get("block")), _int(mine.get("sentence")), norm(mine.get("text"))) != (old["block"], old["sentence"], old["text"]):
                return "An existing footnote was changed or removed."
        new = [n for n in notes if norm(n.get("id")) not in {o["id"] for o in self.docs["footnote"]["footnotes"]}]
        if len(new) != 1:
            return "No new footnote was added." if not new else f"Add exactly one new footnote (this save has {len(new)})."
        note = new[0]
        sentences = split_sentences((blocks.get(norm(note.get("block"))) or {}).get("text"))
        index = _int(note.get("sentence"))
        if not 0 <= index < len(sentences) or sentences[index] != v["sentence_text"]:
            return f"The new footnote is not on the sentence about {task['topic']}."
        if not norm(note.get("text")):
            return "The new footnote is empty."
        if norm(note.get("text")) != v["text"]:
            return "The footnote text does not match the requested text exactly."
        return None

    def act(self, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        if kind != "save":
            return super().act(kind, data)
        s = self.s
        doc = norm(data.get("doc"))
        s.log("page", kind, doc)
        if doc not in self.steps:
            return s.futility(None, "No such document.")
        blocks = {norm(b.get("id")): b for b in data.get("blocks") or [] if isinstance(b, dict)}
        problem = getattr(self, "judge_" + doc)(data, blocks)
        if problem:
            return s.futility(self.msg_keys[doc], "Saved, not done yet: " + problem)
        s.complete(doc)
        return s.say(self.msg_keys[doc], "Saved — " + self.praise[doc])


# =============================================================================================
# 10. Helpdesk — a support ticketing app; tickets, agents and macros live in the harness
# =============================================================================================

_SKILLS = ("Billing", "Hardware", "Network", "Spanish")
_PRIORITIES = ("Urgent", "High", "Normal", "Low")  # shortest first-response SLA first
_DESK_AGENTS = ("Ana Ruiz", "Ben Carter", "Chloe Park", "Diego Santos", "Erin Walsh", "Farah Malik", "Gus Novak", "Hana Sato")
_CUSTOMER_FIRST = (
    "Maria", "James", "Aisha", "Liam", "Sofia", "Noah", "Yuki", "Omar", "Elena", "Lucas", "Grace", "Mateo", "Hannah",
    "Ravi", "Chloe", "Ethan", "Zara", "Felix", "Nora", "Victor", "Ingrid", "Tariq", "Lena", "Jonah", "Mei", "Andre",
    "Clara", "Samir", "Ruby", "Pablo", "Freya", "Kofi", "Alma", "Hugo", "Leah", "Dmitri", "Iris", "Malik", "Rosa", "Theo",
)
_CUSTOMER_LAST = (
    "Lopez", "Okafor", "Bennett", "Kowalski", "Ferreira", "Nakamura", "Haddad", "Lindqvist", "Moreau", "Patel", "Schultz",
    "Rossi", "Chen", "Dubois", "Andersen", "Costa", "Murphy", "Ibrahim", "Weber", "Silva", "Kim", "Larsen", "Fischer",
    "Ramos", "Yilmaz", "Brooks", "Varga", "Quinn",
)
_CUSTOMER_DOMAINS = ("mailbox.example", "inbox.example", "postbox.example", "webmail.example")
HELPDESK_VIEWS = (("unassigned", "Unassigned"), ("duplicates", "Possible duplicates"), ("refunds", "Refund requests"),
                  ("sla", "First response SLA"), ("pending", "Pending customer"))
_SKILL_TICKETS = {  # (subject, message, specific tag): each needs exactly one skill
    "Billing": (
        ("Invoice shows our old company name", "Our March invoice still has our old company name. Can you reissue it with the new one?", "invoice"),
        ("Charged after cancelling", "I cancelled my plan last week but my card was charged again yesterday. Please check.", "payment"),
        ("Need copies of last year's invoices", "Our accountant needs every invoice from last year as PDFs.", "invoice"),
    ),
    "Hardware": (
        ("Laptop won't charge", "My work laptop stopped charging this morning. The charger light is on but the battery stays at 3%.", "laptop"),
        ("Dock ignores my second monitor", "Since I moved desks the docking station only shows one of my two monitors.", "dock"),
        ("Printer jams on every page", "The 3rd floor printer jams on every page, even after reloading the paper.", "printer"),
    ),
    "Network": (
        ("VPN drops every few minutes", "The VPN disconnects every few minutes when I work from home.", "vpn"),
        ("No Wi-Fi in meeting room B", "Laptops can't join the office Wi-Fi in meeting room B.", "wifi"),
        ("Can't reach the file server", "Since this morning I can't reach the shared drive from the office network.", "lan"),
    ),
    "Spanish": (
        ("Pregunta sobre mi pedido", "Hola, hice un pedido hace una semana y todavía no ha llegado. ¿Me pueden decir cuándo llega?", "es"),
        ("No encuentro mi perfil", "Hola, me registré ayer pero no encuentro mi perfil en la aplicación. ¿Me pueden ayudar?", "es"),
        ("Cambiar el idioma de la cuenta", "Buenos días, ¿cómo puedo cambiar el idioma de mi cuenta a español?", "es"),
    ),
}
_DUP_ISSUES = (  # (issue, ((subject, message), (subject, message))): two wordings of one issue
    ("reset", (("Password reset email never arrives", "I asked for a password reset three times and no email arrives, not even in spam."),
               ("Still no reset link", "Following up: I still haven't received any password reset email."))),
    ("double", (("Charged twice for one order", "My card was charged twice for the same order yesterday."),
                ("Double charge on my card", "I see two identical charges for my order on my statement. Please refund one."))),
    ("crash", (("App crashes on startup", "The mobile app closes immediately every time I open it."),
               ("App won't open", "The app still crashes as soon as I launch it, even after reinstalling."))),
    ("address", (("Wrong delivery address", "I entered my old address on my order. Can you change it to my new one?"),
                 ("Please update shipping address", "Writing again: my order needs to go to my new address, not the old one."))),
    ("promo", (("Promo code rejected", "The code from your newsletter says invalid at checkout."),
               ("Discount code not working", "Your newsletter discount code still gets rejected at checkout."))),
    ("sms", (("No 2FA text messages", "I'm not receiving the two-factor codes by text, so I can't log in."),
             ("Verification SMS never comes", "Still no SMS with my login verification code."))),
    ("export", (("CSV export fails", "Exporting my report to CSV fails with an error every time."),
                ("Can't export my report", "The CSV export of my monthly report keeps failing."))),
    ("cancel", (("Cancel my subscription", "Please cancel my subscription at the end of this month."),
                ("How do I cancel?", "I want to stop my subscription. How do I cancel it?"))),
)
_REFUND_ITEMS = (("Blender Pro", "89.99"), ("Spare blender jar", "24.99"), ("Travel mug", "19.50"), ("Coffee grinder", "64.00"),
                 ("Chef's knife set", "112.00"), ("Cast-iron pan", "45.95"), ("Electric kettle", "38.75"), ("Toaster", "54.20"),
                 ("Milk frother", "29.90"))
REFUND_MACRO = "refund_issued"
_MACROS = (
    ("refund_issued", "Refund issued", "Hi {first_name}, good news: we have issued a refund of {amount} for order {order_id}. "
                                       "It can take 5-7 business days to appear on your statement. Thanks for your patience!"),
    ("refund_review", "Refund under review", "Hi {first_name}, thanks for your message. Your refund for order {order_id} is under "
                                             "review and we will update you within 2 business days."),
    ("order_number", "Ask for order number", "Hi {first_name}, could you send us your order number so we can look into this?"),
    ("no_reply", "Closing: no reply", "Hi {first_name}, we have not heard back from you, so we are closing this ticket. "
                                      "Reply any time to reopen it."),
)
_PLACEHOLDERS = ("first_name", "order_id", "amount")
_SLA_SUBJECTS = (
    "VPN keeps disconnecting", "Can't open the shared drive", "Payroll export missing", "Laptop fan is very loud",
    "Invoice PDF is blank", "Order stuck in processing", "Login page shows error 502", "Mobile app logs me out",
    "Report totals don't match", "Can't add a new user", "Card declined at checkout", "Items missing from delivery",
    "Password policy question", "API rate limit errors", "Slow page loads since Monday", "Webhook retries failing",
    "Need a W-9 form", "Barcode scanner won't pair", "Calendar sync duplicates events", "Typo on my invoice",
    "Trial extension request", "Data import is stuck", "SSO login loops", "Wrong currency on invoice",
)
_PENDING_ISSUES = (  # (subject, the customer's problem, the agent's first suggestion)
    ("Sync stuck at 99%", "Desktop sync has been stuck at 99% since yesterday.",
     "Please sign out of the desktop app, sign back in, and let the sync run once more."),
    ("Printer shows offline", "Our office printer shows as offline on every laptop.",
     "Please reinstall the printer driver from the link in our help center."),
    ("Can't upload a profile photo", "Uploading a profile photo fails with 'file too large', even for small images.",
     "We fixed an upload bug yesterday. Please clear your browser cache and try again."),
    ("Calendar invites missing", "Meeting invites no longer show up in my calendar.",
     "Please remove the calendar connection under Settings > Integrations and add it again."),
    ("Export button greyed out", "The Export button on the reports page is greyed out for me.",
     "I've given your account the reporting permission. Please sign out and back in."),
    ("Invoices land in spam", "Our customers say our invoice emails land in their spam folders.",
     "Please add the two DNS records from the attached guide to your domain."),
    ("2FA codes rejected", "The app rejects every two-factor code from my authenticator.",
     "Please turn on automatic time on your phone, then try a fresh code."),
    ("Dashboard won't load", "The dashboard spins forever and never loads.",
     "Please try disabling your browser extensions, then reload the dashboard."),
    ("Webhook not firing", "Our order webhook stopped firing two days ago.",
     "Your endpoint returned errors, so the webhook was paused. I've re-enabled it."),
    ("Search returns nothing", "Knowledge base search returns no results for anything.",
     "We rebuilt your search index. Please try a search again."),
)
_SECOND_TRY = ("Thanks for checking. Please also restart the app completely and try once more.",
               "Thanks. Could you try once more in a private browser window?",
               "Understood. I've reset your session on our side, please try again.")
_CONFIRMS = ("That worked, everything is back to normal now. Thank you!",
             "Confirmed: the problem is gone. You can close this ticket.",
             "Yes, it's all fixed on my end now. Thanks for the quick help!",
             "Just tested it again and it works perfectly now. Thanks!")
_WILL_TRY = ("Thanks, I'll try that tomorrow and let you know.",
             "Thanks! I haven't had a chance to test it yet, I'll get back to you.")
_STILL_BROKEN = ("I did exactly that, but I still get the same problem.",
                 "Unfortunately it's still not working after trying those steps.")
_RECURS = ("Sorry to reopen this: since this morning it's broken again, same as before.",
           "It worked for a day, but now the same problem is back.")
_QUESTIONS = ("Before I try: do I need to do this on every device, or just this one?",
              "Where exactly do I find that? I can't see it anywhere.")
_AGENT_FIXED = ("Our engineers deployed a fix last night, so this should be working now. Could you confirm?",
                "This should be fixed now after today's update. Let us know!")
_GLAD = ("Glad to hear it! I'll mark this as solved.", "Great news, thanks for confirming!")
_CONFIRMED_KINDS = ("confirmed", "confirmed_then_thanks", "confirmed_after_retry")
_OPEN_KINDS = ("will_try", "recurred", "agent_says_fixed", "still_broken", "question")


def macro_text(macro_id: str) -> str:
    return next(text for i, _, text in _MACROS if i == macro_id)


def stamp(when: datetime) -> str:
    return when.strftime("%Y-%m-%d %H:%M")


def pending_thread(kind: str, problem: str, fix: str) -> list[tuple[str, str]]:
    """A Pending-customer conversation as (who, text); only the customer's LATEST message decides the case."""
    c, a = "customer", "agent"
    threads = {
        "confirmed": [(c, problem), (a, fix), (c, RNG.choice(_CONFIRMS))],
        "confirmed_then_thanks": [(c, problem), (a, fix), (c, RNG.choice(_CONFIRMS)), (a, RNG.choice(_GLAD))],
        "confirmed_after_retry": [(c, problem), (a, fix), (c, RNG.choice(_STILL_BROKEN)), (a, RNG.choice(_SECOND_TRY)),
                                  (c, RNG.choice(_CONFIRMS))],
        "will_try": [(c, problem), (a, fix), (c, RNG.choice(_WILL_TRY))],
        "recurred": [(c, problem), (a, fix), (c, RNG.choice(_CONFIRMS)), (a, RNG.choice(_GLAD)), (c, RNG.choice(_RECURS))],
        "agent_says_fixed": RNG.choice(([(c, problem), (a, RNG.choice(_AGENT_FIXED))],
                                        [(c, problem), (a, fix), (c, RNG.choice(_STILL_BROKEN)), (a, RNG.choice(_AGENT_FIXED))])),
        "still_broken": [(c, problem), (a, fix), (c, RNG.choice(_STILL_BROKEN))],
        "question": [(c, problem), (a, fix), (c, RNG.choice(_QUESTIONS))],
    }
    return threads[kind]


class Helpdesk(Gen6):
    name = "v6_helpdesk"
    slug = "v6-helpdesk"
    token = "BENCH_PASS v6-helpdesk-6e19d4"
    family = "ticketing"
    steps = ("assign_skills", "merge_duplicates", "macro_reply", "sla_escalate", "close_confirmed")

    def setup(self) -> None:
        day = app_today()
        self.now = datetime(day.year, day.month, day.day, RNG.randint(13, 16), RNG.randint(0, 59))
        self.tickets: list[dict[str, Any]] = []
        people = RNG.sample([(f, l) for f in _CUSTOMER_FIRST for l in _CUSTOMER_LAST], 60)
        self.people = iter((f, l, RNG.choice(_CUSTOMER_DOMAINS)) for f, l in people)  # every requester is a new person
        agents, best = self.build_agents()
        names = [a["name"] for a in agents]
        unassigned = self.build_unassigned()
        pairs, issues = self.build_duplicates(names)
        refund = self.build_refunds(names)
        sla, sla_targets = self.build_sla(names)
        solve_targets = self.build_pending(names)
        self.tickets.sort(key=lambda t: t["created"])  # ids follow creation order, as in a real ticketing system
        tid = RNG.randint(4000, 7000)
        for t in self.tickets:
            tid += RNG.randint(1, 6)
            t["id"] = str(tid)
        refund_ticket, order_id, amount = refund
        self.v = {
            "now": stamp(self.now),
            "now_text": f"{self.now:%a}, {self.now:%b} {self.now.day}, {self.now.year} {self.now:%H:%M}",
            "tickets": self.tickets,
            "agents": agents,
            "skill": {t["id"]: skill for t, skill in unassigned},
            "assign": {t["id"]: best[skill] for t, skill in unassigned},
            "pairs": [[newer["id"], older["id"]] for newer, older in pairs],
            "issue": {t["id"]: issue for t, issue in issues},
            "refund": {"id": refund_ticket["id"], "first_name": refund_ticket["requester"].split()[0],
                       "order_id": order_id, "amount": amount},
            "sla": sla,
            "sla_targets": sorted(t["id"] for t in sla_targets),
            "solve_targets": sorted(t["id"] for t in solve_targets),
        }
        self.sla_original = {t["id"]: {f: t[f] for f in ("priority", "status", "assignee")} for t in self.in_view("sla")}
        self.assert_unique()

    # --- data ---------------------------------------------------------------------------------
    def add(self, view: str, subject: str, thread: list[tuple[str, str | None, datetime, str]], *,
            person: tuple[str, str, str] | None = None, **fields: Any) -> dict[str, Any]:
        """A ticket whose first message (from the requester) opens it; thread rows are (who, author, at, text)."""
        first, last, domain = person or next(self.people)
        requester = f"{first} {last}"
        thread = sorted(thread, key=lambda m: m[2])
        ticket = {
            "id": "", "view": view, "subject": subject, "requester": requester,
            "email": f"{first}.{last}@{domain}".lower(), "created": stamp(thread[0][2]), "updated": stamp(thread[-1][2]),
            "priority": "Normal", "status": "Open", "assignee": "", "tags": [], "group": "Support", "escalated": False,
            "first_response": None, "order": None, "merged_into": None,
            "messages": [{"who": who, "author": author or requester, "at": stamp(at), "text": text} for who, author, at, text in thread],
        }
        ticket.update(fields)
        self.tickets.append(ticket)
        return ticket

    def ago(self, minutes: int) -> datetime:
        return self.now - timedelta(minutes=minutes)

    def build_agents(self) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """Seven agents; for every skill the lowest-loaded agent who has it is at least 7 below the next, so the
        right agent stays the same whatever order the six tickets are assigned in (loads rise by one per ticket)."""
        names = RNG.sample(_DESK_AGENTS, 7)
        for _attempt in range(20000):
            skills = {n: sorted(RNG.sample(_SKILLS, RNG.choice((1, 1, 2, 2, 3))), key=_SKILLS.index) for n in names}
            loads = {n: RNG.randint(2, 30) for n in names}
            best: dict[str, str] = {}
            for skill in _SKILLS:
                holders = sorted((loads[n], n) for n in names if skill in skills[n])
                if len(holders) < 2 or holders[1][0] - holders[0][0] < 7:
                    break
                best[skill] = holders[0][1]
            else:
                lightest = min(names, key=lambda n: loads[n])
                if len(set(best.values())) >= 3 and set(skills[lightest]) != set(_SKILLS):
                    return [{"name": n, "skills": skills[n], "base": loads[n]} for n in names], best
        raise RuntimeError("no agent roster with a clear lowest load per skill")  # pragma: no cover

    def build_unassigned(self) -> list[tuple[dict[str, Any], str]]:
        needs = list(_SKILLS) + RNG.sample(_SKILLS, 2)
        RNG.shuffle(needs)
        pool = {skill: RNG.sample(_SKILL_TICKETS[skill], needs.count(skill)) for skill in _SKILLS}
        out = []
        for skill in needs:
            subject, text, tag = pool[skill].pop()
            t = self.add("unassigned", subject, [("customer", None, self.ago(RNG.randint(20, 360)), text)],
                         tags=[skill.lower(), tag], priority=RNG.choice(_PRIORITIES[1:]))
            out.append((t, skill))
        return out

    def build_duplicates(self, agents: list[str]) -> tuple[list[tuple[dict, dict]], list[tuple[dict, str]]]:
        """Three true pairs (same requester, same issue) among twelve tickets, with near misses both ways."""
        chosen = RNG.sample(_DUP_ISSUES, 7)
        paired, spare = chosen[:3], chosen[3:]
        persons = [next(self.people) for _ in range(5)]
        pairs, issues = [], []

        def add(person: tuple[str, str, str] | None, issue: tuple[str, Any], variant: int, at: datetime) -> dict[str, Any]:
            subject, text = issue[1][variant]
            t = self.add("duplicates", subject, [("customer", None, at, text)], person=person,
                         assignee=RNG.choice([""] + agents), priority=RNG.choice(_PRIORITIES[1:]))
            issues.append((t, issue[0]))
            return t

        for issue, person in zip(paired, persons[:3]):
            older_at = self.ago(RNG.randint(3 * 1440, 12 * 1440))
            older = add(person, issue, 0, older_at)
            newer = add(person, issue, 1, older_at + timedelta(minutes=RNG.randint(120, 2 * 1440)))
            pairs.append((newer, older))
        add(persons[0], spare[0], RNG.randint(0, 1), self.ago(RNG.randint(600, 12 * 1440)))  # same requester, other issue
        add(persons[3], spare[1], RNG.randint(0, 1), self.ago(RNG.randint(600, 12 * 1440)))  # one requester, two issues
        add(persons[3], spare[2], RNG.randint(0, 1), self.ago(RNG.randint(600, 12 * 1440)))
        add(persons[4], paired[0], RNG.randint(0, 1), self.ago(RNG.randint(600, 12 * 1440)))  # same issue, other requester
        add(None, paired[1], RNG.randint(0, 1), self.ago(RNG.randint(600, 12 * 1440)))
        add(None, spare[3], RNG.randint(0, 1), self.ago(RNG.randint(600, 12 * 1440)))
        return pairs, issues

    def build_refunds(self, agents: list[str]) -> tuple[dict[str, Any], str, str]:
        """Three refund tickets with order panels; the task's ticket is the one whose refund is approved."""
        items = RNG.sample(_REFUND_ITEMS, 7)
        subjects = RNG.sample(("Refund for my return", "Where is my refund?", "Refund status"), 3)
        target = None
        for i, (refund_state, lines) in enumerate((("approved", items[:RNG.choice((2, 3))]), ("under review", items[3:5]),
                                                   ("declined: the return has not arrived", items[5:7]))):
            person = next(self.people)
            returned = RNG.choice(lines)
            order_id = f"ORD-{rand_digits(5)}"
            placed = self.ago(RNG.randint(8 * 1440, 25 * 1440))
            text = (f"Hi, I sent back the {returned[0]} from order {order_id} last week (return label RL-{rand_digits(6)}). "
                    f"Could you confirm my refund? Thanks, {person[0]}")
            total = sum((Decimal(price) for _, price in lines), Decimal("0"))
            order = {"id": order_id, "placed": stamp(placed), "items": [{"name": n, "price": p} for n, p in lines],
                     "total": str(total), "refund": {"status": refund_state, "amount": returned[1]}}
            t = self.add("refunds", subjects[i], [("customer", None, self.ago(RNG.randint(60, 3 * 1440)), text)],
                         person=person, order=order, tags=["refund", "returns"], group="Orders", assignee=RNG.choice(agents))
            if i == 0:
                target = (t, order_id, returned[1])
        assert target is not None
        return target

    def build_sla(self, agents: list[str]) -> tuple[dict[str, int], list[dict[str, Any]]]:
        """Twenty open tickets; 3-5 have breached the first-response SLA of their CURRENT priority."""
        sla = {"Urgent": RNG.choice((1, 2)), "High": RNG.choice((4, 6)), "Normal": RNG.choice((8, 12)), "Low": RNG.choice((24, 48))}
        limit = {p: hours * 60 for p, hours in sla.items()}
        plan: list[tuple[str, int, int | None, str | None]] = []  # (priority, age min, first response after min, old priority)
        n_targets = RNG.randint(3, 5)
        raised = RNG.random() < 0.5
        for i in range(n_targets - raised):  # breached: no first response, older than the SLA (one just past it)
            p = RNG.choice(_PRIORITIES)
            plan.append((p, limit[p] + (RNG.randint(1, 9) if i == 0 else RNG.randint(10, 240)), None, None))
        if raised:  # priority went up: breached under the current priority, not under the old one
            old_i = RNG.randint(1, 3)
            cur, old = _PRIORITIES[RNG.randint(0, old_i - 1)], _PRIORITIES[old_i]
            plan.append((cur, RNG.randint(limit[cur] + 5, limit[old] - 5), None, old))
        for _ in range(RNG.randint(1, 2)):  # exactly at the limit: not breached
            p = RNG.choice(_PRIORITIES)
            plan.append((p, limit[p], None, None))
        for _ in range(2):  # answered late: it has a first response, so it no longer counts
            p = RNG.choice(_PRIORITIES)
            extra = RNG.randint(30, 300)
            plan.append((p, limit[p] + extra, limit[p] + RNG.randint(5, extra - 5), None))
        for _ in range(2):  # answered in time
            p = RNG.choice(_PRIORITIES)
            age = limit[p] + RNG.randint(-limit[p] // 2, 200)
            plan.append((p, age, RNG.randint(5, min(age, limit[p]) - 5), None))
        for _ in range(RNG.randint(1, 2)):  # priority went down: past the old SLA, within the current one
            old_i = RNG.randint(0, 2)
            old, cur = _PRIORITIES[old_i], _PRIORITIES[RNG.randint(old_i + 1, 3)]
            plan.append((cur, RNG.randint(limit[old] + 5, limit[cur] - 5), None, old))
        while len(plan) < 20:  # still inside the SLA
            p = RNG.choice(_PRIORITIES)
            plan.append((p, RNG.randint(5, limit[p] - 5), None, None))
        RNG.shuffle(plan)
        targets = []
        for (p, age, answered, old), subject in zip(plan, RNG.sample(_SLA_SUBJECTS, len(plan))):
            agent = RNG.choice(agents)
            created = self.ago(age)
            thread: list[tuple[str, str | None, datetime, str]] = [
                ("customer", None, created, f"{subject}. Can someone take a look, please?")]
            if old:
                thread.append(("event", agent, created + timedelta(minutes=RNG.randint(1, min(age - 1, 50))),
                               f"Priority changed from {old} to {p}"))
            if answered is not None:
                thread.append(("agent", agent, created + timedelta(minutes=answered), "Thanks for reaching out, we're looking into it now."))
            t = self.add("sla", subject, thread, priority=p, assignee=RNG.choice(["", agent]),
                         first_response=stamp(created + timedelta(minutes=answered)) if answered is not None else None,
                         group=RNG.choice(("Support", "Tier 2")))
            if answered is None and age > limit[p]:
                targets.append(t)
        assert len(targets) == n_targets
        return sla, targets

    def build_pending(self, agents: list[str]) -> list[dict[str, Any]]:
        """Ten Pending-customer tickets; 3-5 end with the customer clearly confirming the fix."""
        n = RNG.randint(3, 5)
        order = RNG.sample(_CONFIRMED_KINDS, 3)
        kinds = [order[i % 3] for i in range(n)] + list(_OPEN_KINDS)
        kinds += RNG.sample(_OPEN_KINDS, 10 - len(kinds))
        RNG.shuffle(kinds)
        targets = []
        for kind, (subject, problem, fix) in zip(kinds, RNG.sample(_PENDING_ISSUES, 10)):
            agent = RNG.choice(agents)
            rows = pending_thread(kind, problem, fix)
            span = RNG.randint(2 * 1440, 9 * 1440)
            offsets = [0] + sorted(RNG.sample(range(60, span - 60), len(rows) - 1))
            thread = [(who, agent if who == "agent" else None, self.ago(span - off), text) for (who, text), off in zip(rows, offsets)]
            first_reply = next(at for who, _, at, _ in thread if who == "agent")
            t = self.add("pending", subject, thread, status="Pending", assignee=agent, priority=RNG.choice(_PRIORITIES[1:]),
                         first_response=stamp(first_reply))
            if kind in _CONFIRMED_KINDS:
                targets.append(t)
        return targets

    def assert_unique(self) -> None:
        """Each challenge has exactly one right answer, re-derived from what the page shows."""
        v = self.v
        assert len({t["id"] for t in self.tickets}) == len(self.tickets)
        assert len({t["requester"] for t in self.tickets}) == len({t["email"] for t in self.tickets})
        loads = {a["name"]: a["base"] for a in v["agents"]}
        for tid, skill in v["skill"].items():
            holders = sorted((loads[a["name"]], a["name"]) for a in v["agents"] if skill in a["skills"])
            assert holders[1][0] - holders[0][0] >= 7 and holders[0][1] == v["assign"][tid]
            assert skill.lower() in self.ticket(tid)["tags"]
        groups: dict[tuple[str, str], list[str]] = {}
        for t in self.in_view("duplicates"):
            groups.setdefault((t["email"], v["issue"][t["id"]]), []).append(t["id"])
        assert sorted(len(g) for g in groups.values()) == [1] * 6 + [2] * 3
        assert len(v["pairs"]) == 3 and all(sorted(g) in [sorted(p) for p in v["pairs"]] for g in groups.values() if len(g) == 2)
        assert all(self.ticket(n)["created"] > self.ticket(o)["created"] for n, o in v["pairs"])
        now = datetime.strptime(v["now"], "%Y-%m-%d %H:%M")
        breached = sorted(t["id"] for t in self.in_view("sla") if not t["first_response"]
                          and (now - datetime.strptime(t["created"], "%Y-%m-%d %H:%M")) > timedelta(hours=v["sla"][t["priority"]]))
        assert breached == v["sla_targets"] and 3 <= len(breached) <= 5
        assert 3 <= len(v["solve_targets"]) <= 5 and len(self.in_view("pending")) == 10
        for t in self.in_view("pending"):
            latest = [m["text"] for m in t["messages"] if m["who"] == "customer"][-1]
            assert (latest in _CONFIRMS) == (t["id"] in v["solve_targets"])
        assert "{" not in self.filled_macro()

    # --- views --------------------------------------------------------------------------------
    def public(self) -> dict[str, Any]:
        v = self.v
        loads = self.loads()
        return {
            "now": v["now"], "now_text": v["now_text"], "sla": v["sla"], "refund_ticket": v["refund"]["id"],
            "views": [{"id": i, "label": label} for i, label in HELPDESK_VIEWS],
            "agents": [{"name": a["name"], "skills": a["skills"], "load": loads[a["name"]]} for a in v["agents"]],
            "macros": [{"id": i, "title": title, "text": text} for i, title, text in _MACROS],
            "tickets": v["tickets"],
        }

    def truth(self) -> dict[str, Any]:
        return {k: val for k, val in self.v.items() if k != "tickets"} | {"macro_body": self.filled_macro()}

    def ticket(self, tid: Any) -> dict[str, Any] | None:
        return next((t for t in self.v["tickets"] if t["id"] == norm(tid).lstrip("#")), None)

    def in_view(self, view: str) -> list[dict[str, Any]]:
        return [t for t in self.tickets if t["view"] == view]

    def loads(self) -> dict[str, int]:
        """Open load: each agent's other open tickets plus the Unassigned-view tickets now assigned to them."""
        loads = {a["name"]: a["base"] for a in self.v["agents"]}
        for t in self.in_view("unassigned"):
            if t["assignee"] in loads:
                loads[t["assignee"]] += 1
        return loads

    def filled_macro(self) -> str:
        r = self.v["refund"]
        return macro_text(REFUND_MACRO).format(first_name=r["first_name"], order_id=r["order_id"], amount=f"${r['amount']}")

    # --- judging after every change -----------------------------------------------------------
    def check(self) -> None:
        s, v = self.s, self.v
        if "assign_skills" not in s.done and all(self.ticket(t)["assignee"] == a for t, a in v["assign"].items()):
            s.complete("assign_skills")
            s.say("assign", "Every ticket is with the right agent ✓")
        if "merge_duplicates" not in s.done and all(self.ticket(n)["merged_into"] == o for n, o in v["pairs"]):
            s.complete("merge_duplicates")
            s.say("merge", "All three duplicates are merged ✓")
        sla = self.in_view("sla")
        if ("sla_escalate" not in s.done and sorted(t["id"] for t in sla if t["escalated"]) == v["sla_targets"]
                and all({f: t[f] for f in self.sla_original[t["id"]]} == self.sla_original[t["id"]] for t in sla)):
            s.complete("sla_escalate")
            s.say("sla", "Exactly the breached tickets are escalated ✓")
        if "close_confirmed" not in s.done and sorted(t["id"] for t in self.in_view("pending") if t["status"] == "Solved") == v["solve_targets"]:
            s.complete("close_confirmed")
            s.say("solve", "Every confirmed ticket is solved ✓")

    def change_problem(self, t: dict[str, Any], field: str, value: Any) -> tuple[str, str] | None:
        """What is wrong with one property change, said without naming the right answer."""
        v, tid = self.v, t["id"]
        if t["view"] == "unassigned" and field == "assignee" and value and value != v["assign"][tid]:
            agent = next(a for a in v["agents"] if a["name"] == value)
            if v["skill"][tid] not in agent["skills"]:
                return "assign", f"#{tid}: {value} lacks the skill this ticket needs."
            return "assign", f"#{tid}: {value} has the skill, but another agent with it has a lower load."
        if t["view"] == "sla":
            if field != "escalated" and value != self.sla_original[tid][field]:
                return "sla", f"#{tid}: only Escalated may change here, but its {field} changed."
            if field == "escalated" and value and tid not in v["sla_targets"]:
                if t["first_response"]:
                    return "sla", f"#{tid} already has a first response, so it is not breached."
                return "sla", f"#{tid} has not waited longer than the SLA of its current priority."
        if t["view"] == "pending" and field == "status" and value == "Solved" and tid not in v["solve_targets"]:
            return "solve", f"#{tid}: the customer's latest message does not confirm a fix."
        return None

    def macro_problem(self, body: str) -> str | None:
        r = self.v["refund"]
        text = squash(body)
        left = [p for p in _PLACEHOLDERS if "{" + p + "}" in text]
        if left:
            return "Unfilled placeholders: " + ", ".join("{" + p + "}" for p in left) + "."
        pattern = re.escape(squash(macro_text(REFUND_MACRO)))
        for p in _PLACEHOLDERS:
            pattern = pattern.replace(re.escape("{" + p + "}"), f"(?P<{p}>.+?)")
        m = re.fullmatch(pattern, text)
        if not m:
            return "Not the Refund issued macro's text: keep its wording, replace only the placeholders."
        wrong = [p for p, ok in (
            ("first_name", m["first_name"].strip().lower() == r["first_name"].lower()),
            ("order_id", m["order_id"].strip().lstrip("#").upper() == r["order_id"]),
            ("amount", money(m["amount"]) == money(r["amount"])),
        ) if not ok]
        return ("Wrong value for " + ", ".join("{" + p + "}" for p in wrong) + ".") if wrong else None

    def act(self, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        s, v = self.s, self.v
        if kind not in ("set", "reply", "merge"):
            return super().act(kind, data)
        t = self.ticket(data.get("id"))
        s.log("page", kind, f"{data.get('id')} {data.get('field') or ''} {data.get('into') or ''}"[:60])
        if t is None:
            return s.futility(None, "No such ticket.")
        if t["status"] == "Closed":
            return s.futility(None, f"#{t['id']} is closed.")
        if kind == "set":
            field = norm(data.get("field"))
            value: Any = bool(data.get("value")) if field == "escalated" else norm(data.get("value"))
            allowed = {"assignee": [""] + [a["name"] for a in v["agents"]], "priority": list(_PRIORITIES),
                       "status": ["Open", "Pending", "Solved"], "escalated": [True, False]}
            if value not in allowed.get(field, ()):
                return s.futility(None, "That value is not allowed.")
            t[field] = value
            s.touch()
            self.check()
            problem = self.change_problem(t, field, value)
            return s.futility(*problem) if problem else {"ok": True}
        if kind == "reply":
            body, public = str(data.get("body") or ""), bool(data.get("public"))
            if not squash(body):
                return s.futility(None, "The message is empty.")
            t["messages"].append({"who": "agent" if public else "note", "author": "You", "at": v["now"], "text": body[:2000]})
            t["updated"] = v["now"]
            if public and not t["first_response"]:
                t["first_response"] = v["now"]
            s.touch()
            if t["id"] == v["refund"]["id"]:
                if not public:
                    return s.futility("macro", "That was an internal note; the customer never sees it. Send a public reply.")
                problem = self.macro_problem(body)
                if problem:
                    return s.futility("macro", problem)
                s.complete("macro_reply")
                return s.say("macro", f"Refund reply sent to {t['requester']} ✓")
            if t["view"] == "sla" and public:
                return s.futility("sla", f"#{t['id']}: only Escalated may change here, but a public reply was sent.")
            return {"ok": True, "msg": "Reply sent." if public else "Internal note added."}
        into = self.ticket(data.get("into"))  # merge: the open ticket closes INTO the chosen one
        if into is None:
            return s.futility("merge", "Choose the ticket to merge into.")
        a, b = t["id"], into["id"]
        if into is t:
            return s.futility("merge", "A ticket cannot be merged into itself.")
        if into["status"] == "Closed":
            return s.futility("merge", f"#{b} is already closed.")
        if t["email"] != into["email"]:
            return s.futility("merge", f"#{a} and #{b} have different requesters, so they are not duplicates.")
        if v["issue"].get(a) is None or v["issue"].get(a) != v["issue"].get(b):
            return s.futility("merge", f"#{a} and #{b}: same requester, different issues.")
        if t["created"] < into["created"]:
            return s.futility("merge", f"#{a} is the older ticket: merge the newer one into it.")
        into["messages"].append({"who": "event", "author": "You", "at": v["now"], "text": f"Ticket #{a} was merged into this ticket"})
        into["messages"].extend(dict(m) for m in t["messages"])
        into["updated"] = v["now"]
        t["messages"].append({"who": "event", "author": "You", "at": v["now"], "text": f"Merged into #{b}"})
        t["status"], t["merged_into"] = "Closed", b
        s.say("merge", f"#{a} merged into #{b}.")
        self.check()
        return {"ok": True}


MISSIONS: dict[str, type[Mission]] = {cls.name: cls for cls in (Mail, Shop, Sheet, Travel, Insights, Admin, Calendar, Crm, Docs, Helpdesk)}
