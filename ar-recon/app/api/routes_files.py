"""File upload / listing endpoints."""
from __future__ import annotations
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.core.enums import FileType
from app.db.models import UploadedFile
from app.ingestion.excel_reader import read_headers
from app.schemas.schemas import FileOut
from app.validation.data_quality import check_file_readable
from config.settings import get_settings

router = APIRouter(prefix="/api/files", tags=["files"])


@router.post("/upload", response_model=FileOut)
async def upload_file(
    file: UploadFile = File(...),
    file_type: FileType = Form(...),
    session: Session = Depends(get_session),
):
    settings = get_settings()
    if not file.filename.lower().endswith((".xlsx", ".xls", ".xlsb", ".xlsm")):
        raise HTTPException(400, "Only .xlsx/.xls/.xlsb/.xlsm accepted")
    dest = Path(settings.upload_dir) / file.filename
    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)

    ok, missing = check_file_readable(dest, file_type)
    if not ok:
        raise HTTPException(422, f"Required columns not found for {file_type.value}: {missing}")

    rec = UploadedFile(
        filename=file.filename, stored_path=str(dest),
        file_type=file_type.value, processing_status="VALIDATED",
    )
    session.add(rec)
    session.flush()
    return FileOut(id=rec.id, filename=rec.filename, file_type=rec.file_type,
                   row_count=rec.row_count, processing_status=rec.processing_status)


@router.get("", response_model=list[FileOut])
def list_files(session: Session = Depends(get_session)):
    rows = session.query(UploadedFile).order_by(UploadedFile.id.desc()).all()
    return [FileOut(id=r.id, filename=r.filename, file_type=r.file_type,
                    row_count=r.row_count, processing_status=r.processing_status) for r in rows]


@router.get("/{file_id}/columns")
def file_columns(file_id: int, session: Session = Depends(get_session)):
    """Column-resolution report: what mapped, what's missing, and suggestions.

    Lets the UI (or you) see exactly how a file's headers resolve before running,
    and which alias to add for anything unresolved.
    """
    rec = session.get(UploadedFile, file_id)
    if not rec:
        raise HTTPException(404, "file not found")
    from app.core.enums import FileType
    from app.ingestion.column_mapper import resolution_report
    from app.ingestion.excel_reader import read_headers
    headers = read_headers(rec.stored_path)
    report = resolution_report(headers, FileType(rec.file_type))
    return {"filename": rec.filename, "headers": headers, **report}
