import os
import io
import time
import sys
from datetime import datetime
from typing import Optional, Union

import pandas as pd
from fastapi.responses import Response, FileResponse

from database import get_db
from core.constants import UPLOAD_FOLDER, SCRAPE_RESULTS_FOLDER

def _get_supabase_storage_configured():
    m = sys.modules.get("main")
    if m and hasattr(m, "is_supabase_storage_configured"):
        return m.is_supabase_storage_configured
    from supabase_storage import is_supabase_storage_configured
    return is_supabase_storage_configured

def _get_supabase_download():
    m = sys.modules.get("main")
    if m and hasattr(m, "download_dataset_file"):
        return m.download_dataset_file
    from supabase_storage import download_dataset_file
    return download_dataset_file

def resolve_dataset_file_path(file_path: Optional[str], dataset_id: Optional[Union[int, str]] = None) -> Optional[str]:
    if not file_path:
        return None

    # 1. Handle Supabase Cloud Storage path
    if str(file_path).startswith("supabase://"):
        filename = os.path.basename(file_path.replace("\\", "/"))
        cache_file = os.path.join(UPLOAD_FOLDER, f"supabase_cache_{filename}")
        if os.path.exists(cache_file) and os.path.getsize(cache_file) > 0:
            return cache_file
        
        is_storage_ok = _get_supabase_storage_configured()()
        if is_storage_ok:
            downloader = _get_supabase_download()
            success, content, err = downloader(file_path)
            if success and content:
                os.makedirs(UPLOAD_FOLDER, exist_ok=True)
                with open(cache_file, "wb") as f:
                    f.write(content)
                return cache_file
            else:
                print(f"[SUPABASE STORAGE DOWNLOAD NOTICE] {err}")
        return None

    if os.path.exists(file_path):
        return file_path
    
    filename = os.path.basename(file_path.replace("\\", "/"))
    local_upload = os.path.join(UPLOAD_FOLDER, filename)
    parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    root_mirpur = os.path.join(parent_dir, "coaching_centers_mirpur.xlsx")
    root_sanitized = os.path.join(parent_dir, "sanitized_coaching_centers.xlsx")

    found_path = None
    if os.path.exists(local_upload):
        found_path = local_upload
    elif "mirpur" in filename.lower() and os.path.exists(root_mirpur):
        found_path = root_mirpur
    elif os.path.exists(root_sanitized):
        found_path = root_sanitized
    elif os.path.exists(os.path.join(parent_dir, filename)):
        found_path = os.path.join(parent_dir, filename)

    # If file not found locally on disk, try looking up in Supabase Storage
    is_storage_ok = _get_supabase_storage_configured()()
    if not found_path and is_storage_ok:
        downloader = _get_supabase_download()
        for prefix in ["synced", "admin", "promoted"]:
            remote_path = f"{prefix}/{filename}"
            success, content, _ = downloader(remote_path)
            if success and content:
                os.makedirs(UPLOAD_FOLDER, exist_ok=True)
                with open(local_upload, "wb") as f:
                    f.write(content)
                found_path = local_upload
                break

    if found_path and dataset_id:
        try:
            conn = get_db()
            conn.execute("UPDATE datasets SET file_path = ? WHERE id = ?", (found_path, str(dataset_id)))
            conn.commit()
            conn.close()
        except Exception as e:
            print("[RESOLVE DATASET PATH DB UPDATE ERROR]", e)

    return found_path


def clean_lead_df(df: pd.DataFrame) -> pd.DataFrame:
    """Clean DataFrame to strip float conversion .0 suffixes and NaN strings"""
    df = df.fillna("")
    for col in df.columns:
        df[col] = df[col].astype(str).str.replace(r'\.0$', '', regex=True)
    return df


def generate_pdf_from_df(df: pd.DataFrame, title: str = "MarketingOstad Dataset Export") -> bytes:
    try:
        from reportlab.lib.pagesizes import letter, landscape
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib import colors

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=landscape(letter),
            rightMargin=20,
            leftMargin=20,
            topMargin=20,
            bottomMargin=20
        )
        elements = []
        styles = getSampleStyleSheet()

        title_style = ParagraphStyle(
            'DocTitle',
            parent=styles['Heading1'],
            fontSize=16,
            textColor=colors.HexColor('#0a0e17'),
            spaceAfter=8
        )
        elements.append(Paragraph(f"<b>MarketingOstad — {title}</b>", title_style))
        elements.append(Paragraph(f"Export Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Total Business Items: {len(df)}", styles['Normal']))
        elements.append(Spacer(1, 10))

        cols = list(df.columns)[:7]
        table_data = [[Paragraph(f"<b>{col}</b>", styles['Normal']) for col in cols]]

        for _, row in df.head(300).iterrows():
            row_data = []
            for col in cols:
                val = str(row[col]) if pd.notna(row[col]) and str(row[col]) != "nan" else ""
                if len(val) > 40:
                    val = val[:37] + "..."
                row_data.append(Paragraph(val, styles['Normal']))
            table_data.append(row_data)

        t = Table(table_data, repeatRows=1)
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#06b6d4')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 8),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#cbd5e1')),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f8fafc')])
        ]))
        elements.append(t)
        doc.build(elements)
        buffer.seek(0)
        return buffer.getvalue()
    except Exception as e:
        print("[PDF GENERATION NOTICE]", e)
        header = f"MarketingOstad Dataset Export: {title}\nDate: {datetime.now()}\nTotal Records: {len(df)}\n\n"
        body = df.to_string(index=False)
        return (header + body).encode("utf-8")


def generate_export_response(file_path: str, export_format: str, title: str = "Exported_Dataset"):
    fmt = (export_format or "excel").lower().strip()
    safe_title = "".join(c for c in title if c.isalnum() or c in ("_", "-")).strip() or "Dataset"
    filename_base = f"{safe_title}_{int(time.time())}"

    if file_path.endswith(".csv"):
        df = pd.read_csv(file_path, dtype=str).fillna("")
    else:
        df = pd.read_excel(file_path, dtype=str).fillna("")

    if fmt in ("csv", ".csv"):
        csv_bytes = df.to_csv(index=False).encode("utf-8-sig")
        return Response(
            content=csv_bytes,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename_base}.csv"'}
        )

    elif fmt in ("json", ".json"):
        json_bytes = df.to_json(orient="records", indent=2, force_ascii=False).encode("utf-8")
        return Response(
            content=json_bytes,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename_base}.json"'}
        )

    elif fmt in ("pdf", ".pdf"):
        pdf_bytes = generate_pdf_from_df(df, title=title)
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename_base}.pdf"'}
        )

    else:
        if file_path.endswith(".xlsx") and os.path.exists(file_path):
            return FileResponse(
                file_path,
                filename=f"{filename_base}.xlsx",
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
        out_buf = io.BytesIO()
        with pd.ExcelWriter(out_buf, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Exported_Leads")
        out_buf.seek(0)
        return Response(
            content=out_buf.getvalue(),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename_base}.xlsx"'}
        )
