"""Merchant KYC / verification submission portal.

Flow: draft → (fill 5 steps) → submitted → admin reviews → approved | rejected
"""
import os
import uuid

from flask import (
    Blueprint, abort, redirect, render_template,
    request, url_for,
)
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename

from ..extensions import db
from ..models import KYCApplication, KYCDirector, KYCDocument
from ..utils import verified_required

bp = Blueprint("kyc", __name__, url_prefix="/kyc")

_UPLOAD_FOLDER = None   # resolved at request time via current_app.instance_path
_ALLOWED = {"pdf", "jpg", "jpeg", "png", "doc", "docx"}

DOC_TYPES = [
    ("certificate",         "Certificate of Incorporation"),
    ("form7_8",             "Form 7 / Form 8 (URSB)"),
    ("tin",                 "TIN Certificate (URA)"),
    ("trade_licence",       "Trade Licence"),
    ("annual_returns",      "Latest Annual Returns"),
    ("director_id",         "Director National ID / Passport"),
    ("aml_questionnaire",   "AML/CFT Questionnaire"),
    ("financial_statements","Audited Financial Statements"),
    ("other",               "Other Supporting Document"),
]


def _get_or_create_app() -> KYCApplication:
    app = KYCApplication.query.filter_by(merchant_id=current_user.id).first()
    if not app:
        app = KYCApplication(merchant_id=current_user.id,
                             company_name=current_user.name)
        db.session.add(app)
        db.session.commit()
    return app


def _upload_root() -> str:
    from flask import current_app
    return os.path.join(current_app.instance_path, "kyc_uploads")


def _save_file(f) -> tuple[str, str]:
    """Save uploaded file safely. Returns (original_filename, stored_filename)."""
    orig = secure_filename(f.filename)
    ext = orig.rsplit(".", 1)[-1].lower() if "." in orig else ""
    if ext not in _ALLOWED:
        raise ValueError(f"File type .{ext} not allowed.")
    stored = f"{uuid.uuid4().hex}.{ext}"
    folder = os.path.join(_upload_root(), str(current_user.id))
    os.makedirs(folder, exist_ok=True)
    f.save(os.path.join(folder, stored))
    return orig, stored


# ── Dashboard entry point ──

@bp.get("")
@login_required
@verified_required
def kyc_home():
    app = KYCApplication.query.filter_by(merchant_id=current_user.id).first()
    return render_template("kyc/home.html", app=app, doc_types=DOC_TYPES)


# ── Step 1: Business Information ──

@bp.get("/step/1")
@login_required
@verified_required
def step1():
    app = _get_or_create_app()
    if app.status not in ("draft",):
        return redirect(url_for("kyc.kyc_home"))
    return render_template("kyc/step1.html", app=app)


@bp.post("/step/1")
@login_required
@verified_required
def step1_save():
    app = _get_or_create_app()
    if app.status != "draft": abort(403)
    app.company_name       = request.form.get("company_name", "").strip()
    app.tin                = request.form.get("tin", "").strip()
    app.registration_number = request.form.get("registration_number", "").strip()
    app.date_of_incorporation = request.form.get("date_of_incorporation", "").strip()
    app.physical_address   = request.form.get("physical_address", "").strip()
    app.contact_phone      = request.form.get("contact_phone", "").strip()
    app.service_type       = request.form.get("service_type", "both")
    _touch(app)
    return redirect(url_for("kyc.step2"))


# ── Step 2: Directors / Signatories ──

@bp.get("/step/2")
@login_required
@verified_required
def step2():
    app = _get_or_create_app()
    return render_template("kyc/step2.html", app=app)


@bp.post("/step/2/add")
@login_required
@verified_required
def step2_add_director():
    app = _get_or_create_app()
    if app.status != "draft": abort(403)
    director = KYCDirector(
        application_id=app.id,
        full_name=request.form.get("full_name", "").strip(),
        date_of_birth=request.form.get("date_of_birth", "").strip(),
        city_of_birth=request.form.get("city_of_birth", "").strip(),
        nationality=request.form.get("nationality", "").strip(),
        id_type=request.form.get("id_type", "national_id"),
        id_number=request.form.get("id_number", "").strip(),
        contact_phone=request.form.get("contact_phone", "").strip(),
        email=request.form.get("email", "").strip(),
        is_primary=bool(request.form.get("is_primary")),
    )
    db.session.add(director)
    _touch(app)
    return redirect(url_for("kyc.step2"))


@bp.post("/step/2/delete/<int:director_id>")
@login_required
@verified_required
def step2_delete_director(director_id: int):
    app = _get_or_create_app()
    d = KYCDirector.query.filter_by(id=director_id, application_id=app.id).first_or_404()
    db.session.delete(d)
    db.session.commit()
    return redirect(url_for("kyc.step2"))


@bp.post("/step/2/next")
@login_required
@verified_required
def step2_next():
    _touch(_get_or_create_app())
    return redirect(url_for("kyc.step3"))


# ── Step 3: Document Upload ──

@bp.get("/step/3")
@login_required
@verified_required
def step3():
    app = _get_or_create_app()
    return render_template("kyc/step3.html", app=app, doc_types=DOC_TYPES)


@bp.post("/step/3/upload")
@login_required
@verified_required
def step3_upload():
    app = _get_or_create_app()
    if app.status != "draft": abort(403)
    f = request.files.get("document")
    doc_type = request.form.get("doc_type", "other")
    if not f or not f.filename:
        return render_template("kyc/step3.html", app=app, doc_types=DOC_TYPES,
                               error="No file selected.")
    try:
        orig, stored = _save_file(f)
    except ValueError as exc:
        return render_template("kyc/step3.html", app=app, doc_types=DOC_TYPES,
                               error=str(exc))
    doc = KYCDocument(application_id=app.id, doc_type=doc_type,
                      original_filename=orig, stored_filename=stored)
    db.session.add(doc)
    _touch(app)
    return redirect(url_for("kyc.step3"))


@bp.post("/step/3/delete/<int:doc_id>")
@login_required
@verified_required
def step3_delete(doc_id: int):
    app = _get_or_create_app()
    doc = KYCDocument.query.filter_by(id=doc_id, application_id=app.id).first_or_404()
    # Delete file from disk
    path = os.path.join(_UPLOAD_FOLDER, str(current_user.id), doc.stored_filename)
    path = os.path.join(_upload_root(), str(current_user.id), doc.stored_filename)
    if os.path.exists(path):
        os.remove(path)
    db.session.delete(doc)
    db.session.commit()
    return redirect(url_for("kyc.step3"))


@bp.post("/step/3/next")
@login_required
@verified_required
def step3_next():
    _touch(_get_or_create_app())
    return redirect(url_for("kyc.step4"))


# ── Step 4: Bank / Settlement Details ──

@bp.get("/step/4")
@login_required
@verified_required
def step4():
    app = _get_or_create_app()
    return render_template("kyc/step4.html", app=app)


@bp.post("/step/4")
@login_required
@verified_required
def step4_save():
    app = _get_or_create_app()
    if app.status != "draft": abort(403)
    app.bank_name      = request.form.get("bank_name", "").strip()
    app.bank_branch    = request.form.get("bank_branch", "").strip()
    app.account_number = request.form.get("account_number", "").strip()
    app.account_name   = request.form.get("account_name", "").strip()
    _touch(app)
    return redirect(url_for("kyc.step5"))


# ── Step 5: AML/CFT Questionnaire ──

@bp.get("/step/5")
@login_required
@verified_required
def step5():
    app = _get_or_create_app()
    return render_template("kyc/step5.html", app=app)


@bp.post("/step/5")
@login_required
@verified_required
def step5_save():
    app = _get_or_create_app()
    if app.status != "draft": abort(403)
    app.ownership_structure     = request.form.get("ownership_structure", "private")
    app.is_listed               = bool(request.form.get("is_listed"))
    app.fatf_country_exposure   = bool(request.form.get("fatf_country_exposure"))
    app.prior_investigations    = bool(request.form.get("prior_investigations"))
    app.has_compliance_officer  = bool(request.form.get("has_compliance_officer"))
    app.aml_notes               = request.form.get("aml_notes", "").strip()
    _touch(app)
    return redirect(url_for("kyc.review"))


# ── Review & Submit ──

@bp.get("/review")
@login_required
@verified_required
def review():
    app = _get_or_create_app()
    return render_template("kyc/review.html", app=app, doc_types=dict(DOC_TYPES))


@bp.post("/submit")
@login_required
@verified_required
def submit():
    from datetime import datetime, timezone
    app = _get_or_create_app()
    if app.status != "draft":
        return redirect(url_for("kyc.kyc_home"))
    # Server-side completeness check — cannot be bypassed by direct POST
    missing = []
    if not app.company_name:  missing.append("business information")
    if not app.directors:     missing.append("at least one director")
    if not app.documents:     missing.append("supporting documents")
    if not app.bank_name:     missing.append("bank details")
    if not app.ownership_structure: missing.append("AML/CFT questionnaire")
    if missing:
        return render_template("kyc/review.html", app=app,
                               doc_types=dict(DOC_TYPES),
                               error=f"Missing: {', '.join(missing)}")
    app.status = "submitted"
    app.submitted_at = datetime.now(timezone.utc)
    _touch(app)
    return redirect(url_for("kyc.kyc_home"))


# ── Admin review routes ──

@bp.get("/admin/<int:app_id>/document/<int:doc_id>")
@login_required
def admin_download(app_id: int, doc_id: int):
    """Serve a KYC document to admins."""
    if current_user.role != "admin":
        abort(403)
    from flask import send_from_directory
    doc = KYCDocument.query.filter_by(id=doc_id, application_id=app_id).first_or_404()
    folder = os.path.join(_upload_root(), str(
        KYCApplication.query.get(app_id).merchant_id
    ))
    return send_from_directory(folder, doc.stored_filename,
                               download_name=doc.original_filename, as_attachment=True)


@bp.get("/admin/list")
@login_required
def admin_list():
    from ..utils import admin_required as _ar
    if current_user.role != "admin":
        abort(403)
    apps = KYCApplication.query.order_by(KYCApplication.submitted_at.desc()).all()
    return render_template("kyc/admin_list.html", apps=apps)


@bp.get("/admin/<int:app_id>")
@login_required
def admin_detail(app_id: int):
    if current_user.role != "admin":
        abort(403)
    app = db.session.get(KYCApplication, app_id) or abort(404)
    return render_template("kyc/admin_detail.html", app=app, doc_types=dict(DOC_TYPES))


@bp.post("/admin/<int:app_id>/review")
@login_required
def admin_review(app_id: int):
    from datetime import datetime, timezone
    if current_user.role != "admin":
        abort(403)
    app = db.session.get(KYCApplication, app_id) or abort(404)
    action = request.form.get("action")
    if action in ("approve", "reject"):
        app.status = "approved" if action == "approve" else "rejected"
        app.reviewer_notes = request.form.get("notes", "").strip()
        app.reviewed_at = datetime.now(timezone.utc)
        # Update merchant KYC status
        from ..models import Merchant
        m = db.session.get(Merchant, app.merchant_id)
        if m:
            m.kyc_status = "verified" if action == "approve" else "rejected"
        db.session.commit()
    return redirect(url_for("kyc.admin_detail", app_id=app_id))


def _touch(app: KYCApplication) -> None:
    from datetime import datetime, timezone
    app.updated_at = datetime.now(timezone.utc)
    db.session.commit()
