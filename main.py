"""
╔══════════════════════════════════════════════════════════════╗
║           VIXSO — Backend API v3.0                          ║
║   Plataforma de Gestión de Equipos Médicos (Multi-Tenant)   ║
╚══════════════════════════════════════════════════════════════╝

Cambios v3.0:
  - Tarifas de viáticos editables desde la DB (no hardcodeadas)
  - Medio viático con checkbox manual
  - Múltiples ingenieros por orden
  - Múltiples sesiones remotas por orden
  - Estado de clientes con alertas (sin servicio / deuda / etc.)
  - Búsqueda de equipos por cliente y modelo
  - Endpoints de reportes para administración
  - Generación de PDF del informe R-751
  - RLS activo en Supabase

Instalación:
  pip install -r requirements.txt

Correr:
  uvicorn main:app --reload --port 8000
"""

import io
import os
from datetime import date, datetime
from typing import List, Optional

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from PIL import Image
from pydantic import BaseModel
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)
from supabase import Client, create_client

# ─────────────────────────────────────────────
#  CONFIGURACIÓN
# ─────────────────────────────────────────────

SUPABASE_URL    = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY    = os.getenv("SUPABASE_SERVICE_KEY", os.getenv("SUPABASE_KEY", "")).strip()  # service role key
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")

# Anon key (pública) hardcodeada para el frontend
SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InRzbG50bGJwYmt6aXF5Y2prZmpoIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODA1NjMyODUsImV4cCI6MjA5NjEzOTI4NX0.oL3W6JUW3zjNKvRwwnQm2o1jI1xwvzRMkcBd-Nhmp_Y"

if not SUPABASE_URL or not SUPABASE_URL.startswith("https://"):
    raise RuntimeError(f"SUPABASE_URL inválida: '{SUPABASE_URL}'")
if not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_KEY no está configurada")

# Cliente DB: usa service key → bypasea RLS → para todas las queries
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
# Cliente Auth: usa anon key → solo para validar JWT del usuario
supabase_auth: Client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)

app = FastAPI(title="VIXSO API", version="3.0.0")

@app.get("/", include_in_schema=False)
def serve_mobile():
    return FileResponse("vixso_mobile_ui.html")

@app.get("/admin", include_in_schema=False)
def serve_admin():
    return FileResponse("vixso_admin_ui.html")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,   # en .env poner el dominio real en producción
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────
#  AUTH
# ─────────────────────────────────────────────

async def get_current_user(authorization: str = Header(...)):
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Token inválido")
    try:
        user = supabase_auth.auth.get_user(authorization.split("Bearer ")[1])
        if not user or not user.user:
            raise HTTPException(401, "Sesión expirada")
        return user.user
    except Exception:
        raise HTTPException(401, "No autorizado")

async def get_admin_user(current_user=Depends(get_current_user)):
    result = supabase.table("profiles").select("role").eq("id", current_user.id).execute()
    if not result.data or result.data[0]["role"] != "admin":
        raise HTTPException(403, "Se requiere rol administrador")
    return current_user


# ─────────────────────────────────────────────
#  UTILIDADES
# ─────────────────────────────────────────────

def get_company_id(user) -> str:
    result = supabase.table("profiles").select("company_id").eq("id", user.id).execute()
    if not result.data:
        raise HTTPException(400, f"Perfil no encontrado para user_id={user.id}. Verificar RLS y service key.")
    return result.data[0]["company_id"]


def calculate_viatico(company_id: str, km: int) -> float:
    """Lee las tarifas vigentes desde la DB. Fallback a los valores originales."""
    try:
        rate = supabase.table("current_tariff").select("*").eq("company_id", company_id).single().execute().data
        if rate:
            if km <= 150:   return km * float(rate["km_up_to_150"])
            if km <= 400:   return km * float(rate["km_151_to_400"])
            return km * float(rate["km_over_400"])
    except Exception:
        pass
    # Fallback
    if km <= 150:   return km * 583
    if km <= 400:   return km * 279
    return km * 240


def compress_image(file_bytes: bytes, max_kb: int = 300) -> tuple[bytes, int, int]:
    original_kb = len(file_bytes) // 1024
    img = Image.open(io.BytesIO(file_bytes))
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    if max(img.size) > 1920:
        img.thumbnail((1920, 1920), Image.LANCZOS)
    output = io.BytesIO()
    quality = 85
    while True:
        output.seek(0); output.truncate()
        img.save(output, format="JPEG", quality=quality, optimize=True)
        if len(output.getvalue()) // 1024 <= max_kb or quality <= 40:
            break
        quality -= 10
    compressed = output.getvalue()
    return compressed, original_kb, len(compressed) // 1024


def upload_to_storage(bucket: str, path: str, data: bytes, content_type: str = "image/jpeg") -> str:
    supabase.storage.from_(bucket).upload(path, data, {"content-type": content_type})
    return supabase.storage.from_(bucket).get_public_url(path)


def isodate(d: Optional[date]) -> Optional[str]:
    return d.isoformat() if d else None


# ─────────────────────────────────────────────
#  MODELOS
# ─────────────────────────────────────────────

class TariffUpdate(BaseModel):
    effective_date: date
    km_up_to_150: float
    km_151_to_400: float
    km_over_400: float
    notes: Optional[str] = None

class ClientStatusUpdate(BaseModel):
    status: str        # activo | sin_servicio | deuda | otro_proveedor | inactivo
    status_reason: Optional[str] = None

class TemplateCreate(BaseModel):
    modality_id: Optional[str] = None
    brand: str
    model_name: str
    description: Optional[str] = None

class ComponentCreate(BaseModel):
    component_name: str
    component_category: Optional[str] = None
    input_type: str = "select"
    is_required: bool = False
    allows_custom: bool = True
    has_lifecycle: bool = False
    expected_lifespan_months: Optional[int] = None
    lifespan_notes: Optional[str] = None
    sort_order: int = 0

class OptionCreate(BaseModel):
    option_value: str
    option_label: Optional[str] = None
    is_default: bool = False
    sort_order: int = 0

class ComponentSelection(BaseModel):
    template_component_id: str
    selected_option_id: Optional[str] = None
    custom_value: Optional[str] = None
    serial_number: Optional[str] = None
    installation_date: Optional[date] = None
    last_replacement_date: Optional[date] = None
    notes: Optional[str] = None

class BulkSelections(BaseModel):
    selections: List[ComponentSelection]

class ReplacementRegister(BaseModel):
    replacement_date: date
    notes: Optional[str] = None

class AlertDismiss(BaseModel):
    dismiss_reason: Optional[str] = None

class AssetCreate(BaseModel):
    client_id: str
    modality_id: str
    template_id: Optional[str] = None
    brand: str
    model_name: str
    serial_number: str
    installation_date: Optional[date] = None

class MRIOperationalUpsert(BaseModel):
    hospital_lan_ip: Optional[str] = None
    host_sw_version: Optional[str] = None
    last_backup_date: Optional[date] = None
    backup_type: Optional[str] = None
    backup_notes: Optional[str] = None
    chiller_brand: Optional[str] = None
    chiller_model: Optional[str] = None
    chiller_serial: Optional[str] = None
    chiller_provider: Optional[str] = None

class CTOperationalUpsert(BaseModel):
    hospital_lan_ip: Optional[str] = None
    host_sw_version: Optional[str] = None
    last_backup_date: Optional[date] = None
    backup_type: Optional[str] = None
    backup_notes: Optional[str] = None

class GammaOperationalUpsert(BaseModel):
    hospital_lan_ip: Optional[str] = None
    acquisition_sw_version: Optional[str] = None
    last_backup_date: Optional[date] = None
    backup_type: Optional[str] = None
    backup_notes: Optional[str] = None

class CoilCreate(BaseModel):
    coil_model: str
    serial_number: Optional[str] = None
    channels: Optional[int] = None
    notes: Optional[str] = None

class PCCreate(BaseModel):
    pc_role: str
    brand: Optional[str] = None
    model: Optional[str] = None
    os_version: Optional[str] = None
    storage_type: Optional[str] = None
    storage_capacity_gb: Optional[int] = None
    ram_gb: Optional[int] = None
    notes: Optional[str] = None

class WorkOrderCreate(BaseModel):
    client_id: str
    asset_id: Optional[str] = None
    service_type: str
    call_reason: str
    additional_engineer_ids: Optional[List[str]] = None  # ingenieros adicionales

class RemoteSessionCreate(BaseModel):
    remote_tool: str    # TeamViewer | AnyDesk | Ammyy

class RemoteSessionStop(BaseModel):
    notes: str

class WorkOrderComplete(BaseModel):
    work_description: str
    half_viatico: bool = False
    half_viatico_reason: Optional[str] = None

class SparePartCreate(BaseModel):
    part_description: str
    part_number: Optional[str] = None
    quantity: int = 1
    unit_cost: Optional[float] = None
    resets_component_selection_id: Optional[str] = None
    notes: Optional[str] = None

class XRayTubeLog(BaseModel):
    action: str
    brand: Optional[str] = None
    model: Optional[str] = None
    serial_number: Optional[str] = None
    exposure_events: Optional[int] = None
    total_hours: Optional[float] = None

class ExpenseCreate(BaseModel):
    expense_section: str
    expense_type: str
    provider_name: Optional[str] = None
    receipt_number: Optional[str] = None
    amount: float
    receipt_url: Optional[str] = None

class ViaticoAdvanceCreate(BaseModel):
    engineer_id: str
    work_order_id: Optional[str] = None
    type: str
    amount: float
    description: Optional[str] = None
    transfer_date: Optional[date] = None


# ═══════════════════════════════════════════════════════════════
#  1 — HEALTH
# ═══════════════════════════════════════════════════════════════

@app.get("/health")
def health():
    try:
        test = supabase.table("profiles").select("id").limit(1).execute()
        db_ok = len(test.data) > 0
    except Exception:
        db_ok = False
    return {"status": "ok", "version": "3.0.0", "db": db_ok}

@app.get("/debug/key", include_in_schema=False)
def debug_key():
    import base64, json as _json
    def decode_jwt(token):
        try:
            payload = token.split('.')[1]
            payload += '=' * (4 - len(payload) % 4)
            return _json.loads(base64.b64decode(payload))
        except Exception as e:
            return {"error": str(e)}
    def key_info(val): return {"suffix": val[-10:], "len": len(val), "role": decode_jwt(val).get("role")} if len(val) > 10 else {"val": val}
    return {
        "SUPABASE_KEY":             key_info(os.getenv("SUPABASE_KEY", "NO")),
        "SUPABASE_SERVICE_KEY":     key_info(os.getenv("SUPABASE_SERVICE_KEY", "NO")),
        "CLAVE_DE_SERVICIO_SUPABASE": key_info(os.getenv("CLAVE_DE_SERVICIO_SUPABASE", "NO")),
        "active_db_key_role": decode_jwt(SUPABASE_KEY).get("role"),
        "profiles_count": len(supabase.table("profiles").select("id").execute().data),
        "clients_count":  len(supabase.table("clients").select("id").execute().data),
    }

@app.get("/config/public", include_in_schema=False)
def get_public_config():
    return {"supabase_url": SUPABASE_URL, "supabase_key": SUPABASE_ANON_KEY}

@app.get("/me")
def get_my_profile(current_user=Depends(get_current_user)):
    result = supabase.table("profiles").select("*").eq("id", current_user.id).execute()
    if not result.data:
        raise HTTPException(404, "Perfil no encontrado")
    return result.data[0]


# ═══════════════════════════════════════════════════════════════
#  2 — CONFIGURACIÓN GENERAL (admin)
# ═══════════════════════════════════════════════════════════════

@app.get("/config/tariff")
def get_tariff(current_user=Depends(get_current_user)):
    """Tarifa de viáticos vigente y su historial."""
    company_id = get_company_id(current_user)
    current = supabase.table("current_tariff").select("*").eq("company_id", company_id).execute().data
    history = supabase.table("tariff_rates").select("*") \
        .eq("company_id", company_id).order("effective_date", desc=True).execute().data
    return {"current": current[0] if current else None, "history": history}


@app.post("/config/tariff")
def update_tariff(body: TariffUpdate, admin=Depends(get_admin_user)):
    """Actualiza las tarifas de viáticos. Solo administradores."""
    company_id = get_company_id(admin)
    data = body.model_dump()
    data["company_id"] = company_id
    data["effective_date"] = isodate(body.effective_date)
    data["created_by"] = admin.id
    result = supabase.table("tariff_rates").insert(data).execute()
    return {"status": "tarifas actualizadas", "data": result.data[0]}


# ═══════════════════════════════════════════════════════════════
#  3 — PLANTILLAS DE EQUIPOS (admin)
# ═══════════════════════════════════════════════════════════════

@app.get("/templates")
def list_templates(current_user=Depends(get_current_user)):
    return supabase.table("equipment_templates") \
        .select("*, modalities(name)").eq("is_active", True).order("brand").execute().data

@app.post("/templates")
def create_template(t: TemplateCreate, admin=Depends(get_admin_user)):
    data = t.model_dump()
    data["company_id"] = get_company_id(admin)
    result = supabase.table("equipment_templates").insert(data).execute()
    return {"status": "plantilla creada", "template": result.data[0]}

@app.get("/templates/{template_id}")
def get_template(template_id: str, current_user=Depends(get_current_user)):
    template = supabase.table("equipment_templates") \
        .select("*, modalities(name)").eq("id", template_id).single().execute().data
    components = supabase.table("template_components") \
        .select("*, component_options(*)").eq("template_id", template_id) \
        .order("sort_order").execute().data
    for comp in components:
        comp["component_options"] = sorted(comp.get("component_options", []), key=lambda o: o["sort_order"])
    return {"template": template, "components": components}

@app.post("/templates/{template_id}/components")
def add_component(template_id: str, comp: ComponentCreate, admin=Depends(get_admin_user)):
    data = comp.model_dump(); data["template_id"] = template_id
    result = supabase.table("template_components").insert(data).execute()
    return {"status": "componente agregado", "component": result.data[0]}

@app.put("/templates/{template_id}/components/{comp_id}")
def update_component(template_id: str, comp_id: str, comp: ComponentCreate, admin=Depends(get_admin_user)):
    result = supabase.table("template_components").update(comp.model_dump()).eq("id", comp_id).execute()
    return {"status": "actualizado", "component": result.data[0]}

@app.delete("/templates/{template_id}/components/{comp_id}")
def delete_component(template_id: str, comp_id: str, admin=Depends(get_admin_user)):
    supabase.table("template_components").delete().eq("id", comp_id).execute()
    return {"status": "componente eliminado"}

@app.post("/templates/{template_id}/components/{comp_id}/options")
def add_option(template_id: str, comp_id: str, opt: OptionCreate, admin=Depends(get_admin_user)):
    data = opt.model_dump(); data["template_component_id"] = comp_id
    result = supabase.table("component_options").insert(data).execute()
    return {"status": "opción agregada", "option": result.data[0]}

@app.delete("/templates/{template_id}/components/{comp_id}/options/{opt_id}")
def delete_option(template_id: str, comp_id: str, opt_id: str, admin=Depends(get_admin_user)):
    supabase.table("component_options").delete().eq("id", opt_id).execute()
    return {"status": "opción eliminada"}


# ═══════════════════════════════════════════════════════════════
#  4 — CLIENTES
# ═══════════════════════════════════════════════════════════════

@app.get("/clients")
def list_clients(status: Optional[str] = None, search: Optional[str] = None,
                 current_user=Depends(get_current_user)):
    """Lista clientes. Filtrable por status y búsqueda de texto."""
    q = supabase.table("clients").select("*")
    if status:
        q = q.eq("status", status)
    if search:
        q = q.ilike("name", f"%{search}%")
    return q.order("name").execute().data

@app.get("/clients/alerts")
def get_client_alerts(current_user=Depends(get_current_user)):
    """Clientes con estado distinto de 'activo' — para el panel de alertas."""
    return supabase.table("clients_with_alert").select("*").execute().data

@app.patch("/clients/{client_id}/status")
def update_client_status(client_id: str, body: ClientStatusUpdate,
                         admin=Depends(get_admin_user)):
    """Marca un cliente como sin servicio, con deuda, etc."""
    result = supabase.table("clients").update({
        "status": body.status,
        "status_reason": body.status_reason,
        "status_updated_at": datetime.utcnow().isoformat(),
        "status_updated_by": admin.id,
    }).eq("id", client_id).execute()
    return {"status": "estado actualizado", "client": result.data[0]}


# ═══════════════════════════════════════════════════════════════
#  5 — EQUIPOS (ASSETS)
# ═══════════════════════════════════════════════════════════════

@app.get("/assets")
def list_assets(client_id: Optional[str] = None,
                model: Optional[str] = None,
                brand: Optional[str] = None,
                search: Optional[str] = None,
                current_user=Depends(get_current_user)):
    """
    Lista equipos con múltiples filtros.
    Un cliente puede tener más de un equipo — todos aparecen.
    """
    q = supabase.table("medical_assets") \
        .select("id, brand, model_name, serial_number, installation_date, template_id, clients(name, city, status), modalities(name)")
    if client_id: q = q.eq("client_id", client_id)
    if brand:     q = q.ilike("brand", f"%{brand}%")
    if model:     q = q.ilike("model_name", f"%{model}%")
    if search:    q = q.or_(f"model_name.ilike.%{search}%,brand.ilike.%{search}%,serial_number.ilike.%{search}%")
    return q.order("brand").execute().data

@app.post("/assets")
def create_asset(asset: AssetCreate, current_user=Depends(get_current_user)):
    data = asset.model_dump()
    data["company_id"] = get_company_id(current_user)
    data["installation_date"] = isodate(asset.installation_date)
    result = supabase.table("medical_assets").insert(data).execute()
    components = []
    if asset.template_id:
        components = supabase.table("template_components") \
            .select("*, component_options(*)").eq("template_id", asset.template_id) \
            .order("sort_order").execute().data
    return {"status": "equipo registrado", "asset": result.data[0], "components_to_fill": components}

@app.get("/assets/search")
def search_assets(q: str, current_user=Depends(get_current_user)):
    """Búsqueda rápida por texto: cliente, modelo, serie, marca."""
    return supabase.table("medical_assets") \
        .select("id, brand, model_name, serial_number, clients(name, city)") \
        .or_(f"model_name.ilike.%{q}%,brand.ilike.%{q}%,serial_number.ilike.%{q}%") \
        .limit(20).execute().data

@app.get("/assets/by-serial/{serial_number}")
def get_by_serial(serial_number: str, current_user=Depends(get_current_user)):
    result = supabase.table("medical_assets") \
        .select("*, clients(name, city, distance_km, status), modalities(name)") \
        .eq("serial_number", serial_number).single().execute()
    if not result.data:
        raise HTTPException(404, "Equipo no encontrado")
    return result.data

@app.get("/assets/{asset_id}")
def get_asset(asset_id: str, current_user=Depends(get_current_user)):
    asset = supabase.table("medical_assets") \
        .select("*, clients(*, status, status_reason), modalities(name), equipment_templates(brand, model_name)") \
        .eq("id", asset_id).single().execute().data
    technical = supabase.table("asset_technical_sheet").select("*").eq("asset_id", asset_id).execute().data
    alerts = [t for t in technical if t["alert_status"] != "ok" and not t["alert_dismissed"]]
    coils  = supabase.table("equipment_coils").select("*").eq("asset_id", asset_id).execute().data
    pcs    = supabase.table("equipment_pcs").select("*").eq("asset_id", asset_id).execute().data
    return {"asset": asset, "technical_sheet": technical,
            "active_alerts": len(alerts), "alerts": alerts, "coils": coils, "pcs": pcs}

@app.post("/assets/{asset_id}/selections")
def save_selections(asset_id: str, body: BulkSelections, current_user=Depends(get_current_user)):
    rows = []
    for s in body.selections:
        row = {"asset_id": asset_id, "template_component_id": s.template_component_id,
               "selected_option_id": s.selected_option_id, "custom_value": s.custom_value,
               "serial_number": s.serial_number, "installation_date": isodate(s.installation_date),
               "notes": s.notes}
        if s.last_replacement_date:
            row["last_replacement_date"] = isodate(s.last_replacement_date)
            comp = supabase.table("template_components").select("expected_lifespan_months") \
                .eq("id", s.template_component_id).single().execute().data
            if comp and comp.get("expected_lifespan_months"):
                from datetime import timedelta
                next_due = s.last_replacement_date + timedelta(days=comp["expected_lifespan_months"] * 30)
                row["next_due_date"] = isodate(next_due)
        rows.append(row)
    result = supabase.table("asset_component_selections") \
        .upsert(rows, on_conflict="asset_id,template_component_id").execute()
    return {"status": "selecciones guardadas", "total": len(result.data)}

@app.patch("/assets/{asset_id}/selections/{sel_id}/replacement")
def register_replacement(asset_id: str, sel_id: str, body: ReplacementRegister,
                         current_user=Depends(get_current_user)):
    supabase.table("asset_component_selections").update({
        "last_replacement_date": isodate(body.replacement_date),
        "notes": body.notes,
        "updated_at": datetime.utcnow().isoformat(),
    }).eq("id", sel_id).eq("asset_id", asset_id).execute()
    updated = supabase.table("asset_component_selections").select("next_due_date, alert_status") \
        .eq("id", sel_id).single().execute().data
    return {"status": "reemplazo registrado", "proxima_revision": updated.get("next_due_date"),
            "estado": updated.get("alert_status")}

@app.patch("/assets/{asset_id}/selections/{sel_id}/dismiss")
def dismiss_alert(asset_id: str, sel_id: str, body: AlertDismiss, current_user=Depends(get_current_user)):
    supabase.table("asset_component_selections").update({
        "alert_dismissed": True, "dismissed_by": current_user.id,
        "dismissed_at": datetime.utcnow().isoformat(), "dismiss_reason": body.dismiss_reason,
    }).eq("id", sel_id).execute()
    return {"status": "alerta reconocida"}

# Operational details
@app.post("/assets/{asset_id}/mri-operational")
def upsert_mri(asset_id: str, body: MRIOperationalUpsert, current_user=Depends(get_current_user)):
    data = body.model_dump(); data["asset_id"] = asset_id
    data["last_backup_date"] = isodate(body.last_backup_date)
    result = supabase.table("mri_operational").upsert(data, on_conflict="asset_id").execute()
    return {"status": "guardado", "data": result.data[0]}

@app.post("/assets/{asset_id}/ct-operational")
def upsert_ct(asset_id: str, body: CTOperationalUpsert, current_user=Depends(get_current_user)):
    data = body.model_dump(); data["asset_id"] = asset_id
    data["last_backup_date"] = isodate(body.last_backup_date)
    result = supabase.table("ct_operational").upsert(data, on_conflict="asset_id").execute()
    return {"status": "guardado", "data": result.data[0]}

@app.post("/assets/{asset_id}/gamma-operational")
def upsert_gamma(asset_id: str, body: GammaOperationalUpsert, current_user=Depends(get_current_user)):
    data = body.model_dump(); data["asset_id"] = asset_id
    data["last_backup_date"] = isodate(body.last_backup_date)
    result = supabase.table("gamma_operational").upsert(data, on_conflict="asset_id").execute()
    return {"status": "guardado", "data": result.data[0]}

# Coils & PCs
@app.get("/assets/{asset_id}/coils")
def list_coils(asset_id: str, current_user=Depends(get_current_user)):
    return supabase.table("equipment_coils").select("*").eq("asset_id", asset_id).execute().data

@app.post("/assets/{asset_id}/coils")
def add_coil(asset_id: str, coil: CoilCreate, current_user=Depends(get_current_user)):
    data = coil.model_dump(); data["asset_id"] = asset_id
    return supabase.table("equipment_coils").insert(data).execute().data[0]

@app.delete("/assets/{asset_id}/coils/{coil_id}")
def delete_coil(asset_id: str, coil_id: str, current_user=Depends(get_current_user)):
    supabase.table("equipment_coils").delete().eq("id", coil_id).execute()
    return {"status": "eliminada"}

@app.get("/assets/{asset_id}/pcs")
def list_pcs(asset_id: str, current_user=Depends(get_current_user)):
    return supabase.table("equipment_pcs").select("*").eq("asset_id", asset_id).execute().data

@app.post("/assets/{asset_id}/pcs")
def add_pc(asset_id: str, pc: PCCreate, current_user=Depends(get_current_user)):
    data = pc.model_dump(); data["asset_id"] = asset_id
    return supabase.table("equipment_pcs").insert(data).execute().data[0]

@app.put("/assets/{asset_id}/pcs/{pc_id}")
def update_pc(asset_id: str, pc_id: str, pc: PCCreate, current_user=Depends(get_current_user)):
    return supabase.table("equipment_pcs").update(pc.model_dump()).eq("id", pc_id).execute().data[0]


# ═══════════════════════════════════════════════════════════════
#  6 — ÓRDENES DE TRABAJO
# ═══════════════════════════════════════════════════════════════

@app.post("/work-orders")
def create_work_order(order: WorkOrderCreate, current_user=Depends(get_current_user)):
    """
    Crea una orden. Calcula viático con las tarifas vigentes de la DB.
    Verifica si el cliente tiene alguna alerta de estado activa.
    Permite agregar ingenieros adicionales al mismo tiempo.
    """
    company_id = get_company_id(current_user)

    client_r = supabase.table("clients").select("distance_km, name, status, status_reason") \
        .eq("id", order.client_id).execute()
    if not client_r.data:
        raise HTTPException(404, "Cliente no encontrado")
    client = client_r.data[0]

    asset = None
    if order.asset_id:
        r = supabase.table("medical_assets").select("modality_id, brand, model_name") \
            .eq("id", order.asset_id).execute()
        asset = r.data[0] if r.data else None

    viatico = calculate_viatico(company_id, client["distance_km"])

    data = {
        "client_id": order.client_id, "asset_id": order.asset_id,
        "service_type": order.service_type, "call_reason": order.call_reason,
        "company_id": company_id, "assigned_engineer_id": current_user.id,
        "viatico_amount": viatico, "status": "Abierto",
    }
    result = supabase.table("work_orders").insert(data).execute()
    new_order = result.data[0]

    # Agregar ingenieros adicionales
    if order.additional_engineer_ids:
        engineers_data = [{"work_order_id": new_order["id"], "engineer_id": eid, "is_lead": False}
                          for eid in order.additional_engineer_ids]
        supabase.table("work_order_engineers").insert(engineers_data).execute()

    # Sugerir especialistas
    specialists = supabase.table("engineer_specialties") \
        .select("user_id, profiles(full_name, phone)") \
        .eq("modality_id", asset["modality_id"]).execute().data

    response = {
        "status": "orden_creada",
        "order_number": new_order["order_number"],
        "order_id": new_order["id"],
        "client": client["name"],
        "viatico_calculado": viatico,
        "ingenieros_sugeridos": [{"id": r["user_id"], "name": r["profiles"]["full_name"]}
                                  for r in specialists],
    }

    # Advertir si el cliente tiene alguna alerta
    if client["status"] != "activo":
        response["client_alert"] = {
            "status": client["status"],
            "reason": client["status_reason"],
            "warning": f"⚠ Este cliente está marcado como '{client['status']}'"
        }
    return response

@app.get("/work-orders")
def list_work_orders(status: Optional[str] = None, engineer_id: Optional[str] = None,
                     client_id: Optional[str] = None, current_user=Depends(get_current_user)):
    q = supabase.table("work_order_summary").select("*")
    if status:      q = q.eq("status", status)
    if client_id:   q = q.eq("client_id", client_id)
    return q.order("created_at", desc=True).execute().data

@app.get("/work-orders/{order_id}")
def get_work_order(order_id: str, current_user=Depends(get_current_user)):
    order    = supabase.table("work_orders").select("*").eq("id", order_id).single().execute().data
    expenses = supabase.table("ticket_expenses").select("*").eq("work_order_id", order_id).execute().data
    parts    = supabase.table("spare_parts_used").select("*").eq("work_order_id", order_id).execute().data
    photos   = supabase.table("asset_media").select("id, caption, storage_path, compressed_size_kb") \
        .eq("work_order_id", order_id).execute().data
    xray     = supabase.table("xray_tube_log").select("*").eq("work_order_id", order_id).execute().data
    sessions = supabase.table("remote_sessions").select("*, profiles(full_name)") \
        .eq("work_order_id", order_id).order("start_time").execute().data
    engineers = supabase.table("work_order_engineers").select("*, profiles(full_name, phone)") \
        .eq("work_order_id", order_id).execute().data

    total_exp = sum(e["amount"] for e in expenses)
    viatico_efectivo = (order.get("viatico_amount") or 0) / 2 if order.get("half_viatico") else (order.get("viatico_amount") or 0)

    return {
        "order": order, "expenses": expenses, "total_expenses": total_exp,
        "viatico_efectivo": viatico_efectivo,
        "viatico_balance": viatico_efectivo - (order.get("viatico_advance") or 0) - total_exp,
        "spare_parts": parts, "photos": photos, "xray_tube_log": xray,
        "remote_sessions": sessions, "additional_engineers": engineers,
    }

@app.post("/work-orders/{order_id}/engineers")
def add_engineer_to_order(order_id: str, engineer_id: str, current_user=Depends(get_current_user)):
    """Agrega un segundo ingeniero a una orden ya creada."""
    supabase.table("work_order_engineers").insert({
        "work_order_id": order_id, "engineer_id": engineer_id, "is_lead": False
    }).execute()
    return {"status": "ingeniero agregado"}

# — Sesiones remotas (múltiples por orden) —
@app.post("/work-orders/{order_id}/remote-sessions")
def start_remote_session(order_id: str, session: RemoteSessionCreate, current_user=Depends(get_current_user)):
    """Inicia una nueva sesión de soporte remoto. Puede haber varias por orden."""
    result = supabase.table("remote_sessions").insert({
        "work_order_id": order_id, "engineer_id": current_user.id,
        "remote_tool": session.remote_tool, "start_time": datetime.utcnow().isoformat(),
    }).execute()
    supabase.table("work_orders").update({"status": "En Soporte Remoto"}).eq("id", order_id).execute()
    return {"status": "sesión iniciada", "session_id": result.data[0]["id"]}

@app.patch("/work-orders/{order_id}/remote-sessions/{session_id}/stop")
def stop_remote_session(order_id: str, session_id: str, body: RemoteSessionStop,
                        current_user=Depends(get_current_user)):
    """Detiene una sesión remota y calcula los minutos."""
    session = supabase.table("remote_sessions").select("start_time") \
        .eq("id", session_id).single().execute().data
    end = datetime.utcnow()
    start = datetime.fromisoformat(session["start_time"].replace("Z", "+00:00"))
    minutes = int((end - start.replace(tzinfo=None)).total_seconds() / 60)
    supabase.table("remote_sessions").update({
        "end_time": end.isoformat(), "duration_minutes": minutes, "notes": body.notes,
    }).eq("id", session_id).execute()
    return {"status": "sesión detenida", "minutos": minutes}

@app.get("/work-orders/{order_id}/remote-sessions")
def list_remote_sessions(order_id: str, current_user=Depends(get_current_user)):
    sessions = supabase.table("remote_sessions").select("*, profiles(full_name)") \
        .eq("work_order_id", order_id).order("start_time").execute().data
    total_min = sum(s.get("duration_minutes") or 0 for s in sessions)
    return {"sessions": sessions, "total_minutes": total_min}

@app.patch("/work-orders/{order_id}/complete")
def complete_order(order_id: str, data: WorkOrderComplete, current_user=Depends(get_current_user)):
    """
    Cierra la orden con descripción del trabajo.
    El técnico indica si aplica medio viático (múltiples servicios en el día).
    El reporte se genera con los datos del perfil del técnico.
    """
    supabase.table("work_orders").update({
        "status": "Completado",
        "work_description": data.work_description,
        "service_end_time": datetime.utcnow().isoformat(),
        "half_viatico": data.half_viatico,
        "half_viatico_reason": data.half_viatico_reason,
    }).eq("id", order_id).execute()
    return {"status": "orden completada",
            "medio_viatico_aplicado": data.half_viatico}

@app.post("/work-orders/{order_id}/spare-parts")
def add_spare_part(order_id: str, part: SparePartCreate, current_user=Depends(get_current_user)):
    data = part.model_dump()
    reset_id = data.pop("resets_component_selection_id", None)
    data["work_order_id"] = order_id
    result = supabase.table("spare_parts_used").insert(data).execute()
    reset_info = None
    if reset_id:
        wo = supabase.table("work_orders").select("asset_id").eq("id", order_id).single().execute().data
        supabase.table("asset_component_selections").update({
            "last_replacement_date": date.today().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
        }).eq("id", reset_id).eq("asset_id", wo["asset_id"]).execute()
        reset_info = supabase.table("asset_component_selections") \
            .select("next_due_date, alert_status").eq("id", reset_id).single().execute().data
    return {"status": "repuesto registrado", "data": result.data[0], "component_reset": reset_info}

@app.post("/work-orders/{order_id}/xray-tube")
def log_xray_tube(order_id: str, log: XRayTubeLog, current_user=Depends(get_current_user)):
    data = log.model_dump(); data["work_order_id"] = order_id
    return supabase.table("xray_tube_log").insert(data).execute().data[0]

@app.post("/work-orders/{order_id}/expenses")
def add_expense(order_id: str, expense: ExpenseCreate, current_user=Depends(get_current_user)):
    data = expense.model_dump(); data["work_order_id"] = order_id
    return supabase.table("ticket_expenses").insert(data).execute().data[0]


# ═══════════════════════════════════════════════════════════════
#  7 — GENERACIÓN DE PDF (Informe R-751)
# ═══════════════════════════════════════════════════════════════

@app.get("/work-orders/{order_id}/pdf")
def generate_pdf(order_id: str, current_user=Depends(get_current_user)):
    """
    Genera el informe R-751 en PDF.
    Los datos del técnico se toman de su perfil de usuario.
    No requiere firma — el reporte identifica al técnico por nombre y legajo.
    """
    # Cargar todos los datos
    order    = supabase.table("work_orders").select("*").eq("id", order_id).single().execute().data
    client   = supabase.table("clients").select("*").eq("id", order["client_id"]).single().execute().data
    asset    = supabase.table("medical_assets").select("*").eq("id", order["asset_id"]).single().execute().data
    engineer = supabase.table("profiles").select("full_name, phone, legajo") \
        .eq("id", order["assigned_engineer_id"]).single().execute().data
    expenses = supabase.table("ticket_expenses").select("*").eq("work_order_id", order_id).execute().data
    parts    = supabase.table("spare_parts_used").select("*").eq("work_order_id", order_id).execute().data
    sessions = supabase.table("remote_sessions").select("*").eq("work_order_id", order_id).execute().data

    viatico_efectivo = (order.get("viatico_amount") or 0) / 2 \
        if order.get("half_viatico") else (order.get("viatico_amount") or 0)
    total_expenses = sum(e["amount"] for e in expenses)

    # Construir PDF
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4,
                            leftMargin=2*cm, rightMargin=2*cm,
                            topMargin=2*cm, bottomMargin=2*cm)
    styles = getSampleStyleSheet()
    elements = []

    # Estilo título
    title_style = ParagraphStyle("title", parent=styles["Heading1"],
                                 fontSize=14, spaceAfter=4)
    sub_style   = ParagraphStyle("sub", parent=styles["Normal"],
                                 fontSize=9, textColor=colors.grey)
    bold_style  = ParagraphStyle("bold", parent=styles["Normal"],
                                 fontSize=10, fontName="Helvetica-Bold")
    normal      = styles["Normal"]
    normal.fontSize = 10

    # Encabezado
    elements.append(Paragraph("BIONUCLEAR S.A.", title_style))
    elements.append(Paragraph("Informe de Servicio Técnico", title_style))
    elements.append(Paragraph(f"Número de Orden: {order['order_number']}  |  "
                               f"Fecha: {order['created_at'][:10]}  |  "
                               f"Tipo: {order['service_type']}", sub_style))
    elements.append(Spacer(1, 0.4*cm))

    # Sección 1: Datos del cliente y equipo
    elements.append(Paragraph("1. DATOS DEL CLIENTE Y EQUIPO", bold_style))
    info_data = [
        ["Cliente:", client.get("name", ""), "Ciudad:", client.get("city", "")],
        ["Equipo:", f"{asset.get('brand','')} {asset.get('model_name','')}", "S/N:", asset.get("serial_number", "")],
        ["Motivo:", order.get("call_reason", ""), "Técnico:", f"{engineer.get('full_name','')} (Leg. {engineer.get('legajo','')})"],
    ]
    t = Table(info_data, colWidths=[3.5*cm, 7*cm, 2.5*cm, 4*cm])
    t.setStyle(TableStyle([
        ("FONTSIZE", (0,0), (-1,-1), 9),
        ("FONTNAME", (0,0), (0,-1), "Helvetica-Bold"),
        ("FONTNAME", (2,0), (2,-1), "Helvetica-Bold"),
        ("GRID", (0,0), (-1,-1), 0.25, colors.lightgrey),
        ("BACKGROUND", (0,0), (0,-1), colors.Color(0.94, 0.97, 1.0)),
        ("BACKGROUND", (2,0), (2,-1), colors.Color(0.94, 0.97, 1.0)),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0,0), (-1,-1), [colors.white, colors.Color(0.98,0.98,0.98)]),
    ]))
    elements.append(t)
    elements.append(Spacer(1, 0.4*cm))

    # Sección 2: Soporte remoto
    if sessions:
        elements.append(Paragraph("2. SOPORTE REMOTO", bold_style))
        session_data = [["Herramienta", "Inicio", "Fin", "Minutos", "Notas"]]
        total_min = 0
        for s in sessions:
            session_data.append([
                s.get("remote_tool",""),
                (s.get("start_time") or "")[:16].replace("T"," "),
                (s.get("end_time") or "-")[:16].replace("T"," "),
                str(s.get("duration_minutes") or "-"),
                (s.get("notes") or "")[:60],
            ])
            total_min += s.get("duration_minutes") or 0
        session_data.append(["", "", "TOTAL", str(total_min) + " min", ""])
        t2 = Table(session_data, colWidths=[3*cm, 3.5*cm, 3.5*cm, 2*cm, 5*cm])
        t2.setStyle(TableStyle([
            ("FONTSIZE", (0,0), (-1,-1), 8),
            ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
            ("BACKGROUND", (0,0), (-1,0), colors.Color(0.2, 0.4, 0.8)),
            ("TEXTCOLOR", (0,0), (-1,0), colors.white),
            ("GRID", (0,0), (-1,-1), 0.25, colors.lightgrey),
            ("ROWBACKGROUNDS", (0,1), (-1,-2), [colors.white, colors.Color(0.97,0.97,0.97)]),
            ("FONTNAME", (0,-1), (-1,-1), "Helvetica-Bold"),
        ]))
        elements.append(t2)
        elements.append(Spacer(1, 0.4*cm))

    # Sección 3: Descripción del trabajo
    elements.append(Paragraph("3. DESCRIPCIÓN DEL TRABAJO REALIZADO", bold_style))
    elements.append(Paragraph(order.get("work_description") or "(sin descripción)", normal))
    elements.append(Spacer(1, 0.4*cm))

    # Sección 4: Repuestos
    if parts:
        elements.append(Paragraph("4. REPUESTOS UTILIZADOS", bold_style))
        parts_data = [["Descripción", "N° Parte", "Cantidad", "Costo Unit."]]
        for p in parts:
            parts_data.append([p.get("part_description",""), p.get("part_number","-"),
                                str(p.get("quantity",1)), f"${p.get('unit_cost') or '-'}"])
        t3 = Table(parts_data, colWidths=[8*cm, 4*cm, 2.5*cm, 2.5*cm])
        t3.setStyle(TableStyle([
            ("FONTSIZE", (0,0), (-1,-1), 8),
            ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
            ("BACKGROUND", (0,0), (-1,0), colors.Color(0.2, 0.4, 0.8)),
            ("TEXTCOLOR", (0,0), (-1,0), colors.white),
            ("GRID", (0,0), (-1,-1), 0.25, colors.lightgrey),
        ]))
        elements.append(t3)
        elements.append(Spacer(1, 0.4*cm))

    # Sección 6: Gastos y viáticos
    elements.append(Paragraph("6. GASTOS Y VIÁTICOS", bold_style))
    expenses_data = [["Sección", "Tipo", "Proveedor", "N° Comp.", "Importe"]]
    for e in expenses:
        expenses_data.append([e.get("expense_section",""), e.get("expense_type",""),
                               e.get("provider_name","-"), e.get("receipt_number","-"),
                               f"${e.get('amount',0):,.2f}"])
    expenses_data.append(["", "", "", "SUBTOTAL GASTOS", f"${total_expenses:,.2f}"])
    expenses_data.append(["", "", "", "VIÁTICO" + (" (½)" if order.get("half_viatico") else ""),
                           f"${viatico_efectivo:,.2f}"])
    expenses_data.append(["", "", "", "ADELANTO RECIBIDO", f"${order.get('viatico_advance',0):,.2f}"])
    saldo = viatico_efectivo - (order.get("viatico_advance") or 0) - total_expenses
    expenses_data.append(["", "", "", "SALDO A COBRAR", f"${saldo:,.2f}"])

    t4 = Table(expenses_data, colWidths=[3*cm, 3*cm, 4*cm, 3.5*cm, 3.5*cm])
    t4.setStyle(TableStyle([
        ("FONTSIZE", (0,0), (-1,-1), 8),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("BACKGROUND", (0,0), (-1,0), colors.Color(0.2, 0.4, 0.8)),
        ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("GRID", (0,0), (-1,-1), 0.25, colors.lightgrey),
        ("FONTNAME", (3,-4), (-1,-1), "Helvetica-Bold"),
        ("BACKGROUND", (3,-1), (-1,-1), colors.Color(0.0, 0.6, 0.3)),
        ("TEXTCOLOR", (3,-1), (-1,-1), colors.white),
    ]))
    elements.append(t4)
    elements.append(Spacer(1, 0.6*cm))

    # Pie de página con datos del técnico
    elements.append(Paragraph(
        f"Técnico: {engineer.get('full_name','')}  |  "
        f"Legajo: {engineer.get('legajo','')}  |  "
        f"Tel: {engineer.get('phone','')}  |  "
        f"Bionuclear S.A. — L.N. Alem 1748, Dock Sud, Avellaneda",
        ParagraphStyle("footer", parent=styles["Normal"], fontSize=8,
                       textColor=colors.grey, borderTop=0.5, borderPadding=4)
    ))

    doc.build(elements)
    buffer.seek(0)

    filename = f"R751_{order['order_number']}_{order['created_at'][:10]}.pdf"
    return StreamingResponse(
        buffer,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


# ═══════════════════════════════════════════════════════════════
#  8 — GALERÍA DE IMÁGENES
# ═══════════════════════════════════════════════════════════════

@app.post("/assets/{asset_id}/media")
async def upload_asset_photo(asset_id: str, media_type: str, caption: Optional[str] = None,
                             file: UploadFile = File(...), current_user=Depends(get_current_user)):
    raw = await file.read()
    compressed, orig_kb, comp_kb = compress_image(raw)
    path = f"assets/{asset_id}/{datetime.utcnow().timestamp()}_{file.filename}"
    upload_to_storage("equipment-media", path, compressed)
    result = supabase.table("asset_media").insert({
        "asset_id": asset_id, "media_type": media_type, "file_name": file.filename,
        "storage_path": path, "mime_type": "image/jpeg", "original_size_kb": orig_kb,
        "compressed_size_kb": comp_kb, "is_compressed": True, "caption": caption,
        "uploaded_by": current_user.id,
    }).execute()
    return {"status": "foto subida", "id": result.data[0]["id"],
            "reduccion_pct": round((1 - comp_kb / orig_kb) * 100, 1) if orig_kb else 0}

@app.post("/work-orders/{order_id}/photos")
async def upload_service_photo(order_id: str, asset_id: str, caption: Optional[str] = None,
                               file: UploadFile = File(...), current_user=Depends(get_current_user)):
    count = supabase.table("asset_media").select("id", count="exact") \
        .eq("work_order_id", order_id).eq("media_type", "foto_servicio").execute().count
    if count >= 10:
        raise HTTPException(400, "Límite de 10 fotos por servicio")
    raw = await file.read()
    compressed, orig_kb, comp_kb = compress_image(raw)
    path = f"services/{order_id}/{datetime.utcnow().timestamp()}_{file.filename}"
    upload_to_storage("equipment-media", path, compressed)
    result = supabase.table("asset_media").insert({
        "asset_id": asset_id, "work_order_id": order_id, "media_type": "foto_servicio",
        "file_name": file.filename, "storage_path": path, "mime_type": "image/jpeg",
        "original_size_kb": orig_kb, "compressed_size_kb": comp_kb, "is_compressed": True,
        "caption": caption, "uploaded_by": current_user.id,
    }).execute()
    return {"status": "foto subida", "id": result.data[0]["id"]}

@app.get("/assets/{asset_id}/media")
def get_gallery(asset_id: str, current_user=Depends(get_current_user)):
    all_media = supabase.table("asset_media").select("*").eq("asset_id", asset_id) \
        .order("created_at", desc=True).execute().data
    gallery = {}
    for item in all_media:
        gallery.setdefault(item["media_type"], []).append(item)
    return gallery


# ═══════════════════════════════════════════════════════════════
#  9 — ALERTAS
# ═══════════════════════════════════════════════════════════════

@app.get("/alerts")
def get_active_alerts(current_user=Depends(get_current_user)):
    """Panel consolidado: alertas de componentes + clientes sin servicio."""
    component_alerts = supabase.table("active_alerts").select("*").execute().data
    client_alerts    = supabase.table("clients_with_alert").select("*").execute().data
    return {
        "total": len(component_alerts) + len(client_alerts),
        "component_alerts": {"count": len(component_alerts), "items": component_alerts},
        "client_alerts": {"count": len(client_alerts), "items": client_alerts},
    }

@app.post("/admin/refresh-alerts")
def refresh_alerts(admin=Depends(get_admin_user)):
    supabase.rpc("refresh_component_alerts").execute()
    return {"status": "alertas actualizadas", "timestamp": datetime.utcnow().isoformat()}


# ═══════════════════════════════════════════════════════════════
#  10 — VIÁTICOS
# ═══════════════════════════════════════════════════════════════

@app.post("/viaticos/advance")
def register_advance(advance: ViaticoAdvanceCreate, current_user=Depends(get_current_user)):
    data = advance.model_dump()
    data["company_id"] = get_company_id(current_user)
    data["transfer_date"] = isodate(advance.transfer_date)
    return supabase.table("viatico_advances").insert(data).execute().data[0]

@app.get("/viaticos/balance")
def get_all_balances(current_user=Depends(get_current_user)):
    return supabase.table("viatico_balance").select("*").execute().data

@app.get("/viaticos/balance/{engineer_id}")
def get_engineer_balance(engineer_id: str, current_user=Depends(get_current_user)):
    balance = supabase.table("viatico_balance").select("*").eq("engineer_id", engineer_id).single().execute().data
    history = supabase.table("viatico_advances").select("*, work_orders(order_number)") \
        .eq("engineer_id", engineer_id).order("created_at", desc=True).execute().data
    return {"balance": balance, "history": history}


# ═══════════════════════════════════════════════════════════════
#  11 — REPORTES (administración)
# ═══════════════════════════════════════════════════════════════

@app.get("/reports/summary")
def report_summary(date_from: str, date_to: str, current_user=Depends(get_current_user)):
    """Resumen del período: órdenes, horas remotas, gastos totales."""
    orders = supabase.table("work_order_summary").select("*") \
        .gte("created_at", date_from).lte("created_at", date_to).execute().data
    return {
        "periodo": {"desde": date_from, "hasta": date_to},
        "total_ordenes": len(orders),
        "por_estado": {s: sum(1 for o in orders if o["status"] == s)
                       for s in ["Abierto","En Soporte Remoto","Completado"]},
        "por_tipo": {t: sum(1 for o in orders if o["service_type"] == t)
                     for t in ["Correctivo","Preventivo","Garantía","Abono","Otros"]},
        "total_minutos_remotos": sum(o.get("total_remote_minutes") or 0 for o in orders),
        "total_gastos": sum(o.get("total_expenses") or 0 for o in orders),
        "total_viaticos": sum(o.get("viatico_efectivo") or 0 for o in orders),
        "ordenes": orders,
    }

@app.get("/reports/by-engineer")
def report_by_engineer(date_from: str, date_to: str, current_user=Depends(get_current_user)):
    """Resumen por ingeniero: órdenes completadas, horas remotas, viáticos."""
    orders = supabase.table("work_order_summary").select("*") \
        .gte("created_at", date_from).lte("created_at", date_to) \
        .eq("status", "Completado").execute().data
    by_engineer: dict = {}
    for o in orders:
        eng = o.get("lead_engineer") or "Sin asignar"
        if eng not in by_engineer:
            by_engineer[eng] = {"ordenes": 0, "minutos_remotos": 0, "viaticos": 0.0, "gastos": 0.0}
        by_engineer[eng]["ordenes"]         += 1
        by_engineer[eng]["minutos_remotos"] += o.get("total_remote_minutes") or 0
        by_engineer[eng]["viaticos"]        += float(o.get("viatico_efectivo") or 0)
        by_engineer[eng]["gastos"]          += float(o.get("total_expenses") or 0)
    return {"periodo": {"desde": date_from, "hasta": date_to}, "por_ingeniero": by_engineer}

@app.get("/reports/alerts-history")
def report_alerts_history(current_user=Depends(get_current_user)):
    """Componentes con alertas activas o vencidas — para planificación de preventivos."""
    return supabase.table("active_alerts").select("*").execute().data
