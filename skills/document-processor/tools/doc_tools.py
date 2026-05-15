"""
Document Processor Skill — Advanced document processing tools.

These tools extend the built-in read_excel/read_pdf/read_docx/read_pptx
with write, analyze, and convert capabilities.

Requires: openpyxl, pdfplumber, python-docx, python-pptx, pandas, Pillow
Install:  pip install 'phoenix-agent[doc]'
"""

import json
import os
from pathlib import Path
from typing import Optional

from phoenix_agent.tools.registry import (
    ToolResult,
    ToolDefinition,
    ToolCategory,
    ToolRegistry,
)


def _safe_path(raw: str):
    """Resolve and validate a user-supplied path."""
    if ".." in raw:
        return None, "Path traversal ('..') is not allowed"
    try:
        resolved = Path(raw).expanduser().resolve()
        return resolved, None
    except Exception as exc:
        return None, f"Invalid path: {exc}"


def _check_dep(lib_name: str, install_cmd: str) -> Optional[str]:
    """Return error JSON if dependency missing, else None."""
    try:
        __import__(lib_name)
    except ImportError:
        return ToolResult(
            success=False, content="",
            error=f"Missing dependency: {lib_name}. Install: {install_cmd}",
        ).to_json()
    return None


def analyze_excel(file_path: str, aggregation: str = "") -> str:
    """
    Analyze an Excel file: summary statistics, column info, data profiling.

    Args:
        file_path:   Path to the .xlsx file.
        aggregation: Optional aggregation, e.g. 'sum', 'mean', 'count',
                     'describe'. Applied to numeric columns.
    """
    err = _check_dep("openpyxl", "pip install 'phoenix-agent[doc]'")
    if err:
        return err

    path, perr = _safe_path(file_path)
    if perr:
        return ToolResult(success=False, content="", error=perr).to_json()
    if not path or not path.is_file():
        return ToolResult(success=False, content="",
                          error=f"File not found: {file_path}").to_json()

    try:
        import pandas as pd

        # Read all sheets
        xls = pd.ExcelFile(str(path))
        parts = [f"Excel Analysis: {path.name}\n",
                 f"Sheets: {xls.sheet_names}\n"]

        for sheet in xls.sheet_names:
            df = pd.read_excel(str(path), sheet_name=sheet)
            parts.append(f"\n### Sheet: {sheet}")
            parts.append(f"Rows: {len(df)}, Columns: {len(df.columns)}")
            parts.append(f"\nColumns: {list(df.columns)}")

            # Data types
            parts.append("\nData types:")
            for col, dtype in df.dtypes.items():
                non_null = df[col].count()
                parts.append(f"  {col}: {dtype} ({non_null} non-null)")

            # Numeric summary
            numeric = df.select_dtypes(include="number")
            if not numeric.empty:
                parts.append("\nNumeric summary:")
                parts.append(numeric.describe().to_string())

            # Aggregation
            if aggregation:
                valid_agg = ["sum", "mean", "count", "describe",
                             "min", "max", "median", "std"]
                if aggregation in valid_agg:
                    parts.append(f"\nAggregation ({aggregation}):")
                    if numeric.empty:
                        parts.append("  No numeric columns.")
                    else:
                        result = getattr(numeric, aggregation)()
                        if isinstance(result, pd.DataFrame):
                            parts.append(result.to_string())
                        else:
                            parts.append(str(result))
                else:
                    parts.append(f"\nUnknown aggregation: {aggregation}. "
                                 f"Valid: {valid_agg}")

            # First few rows preview
            parts.append(f"\nPreview (first 5 rows):")
            parts.append(df.head().to_string(index=False))

        return ToolResult(
            success=True, content="\n".join(parts),
            metadata={"sheets": xls.sheet_names},
        ).to_json()
    except ImportError:
        return _check_dep("pandas", "pip install 'phoenix-agent[doc]'")
    except Exception as exc:
        return ToolResult(success=False, content="",
                          error=f"Excel analysis error: {exc}").to_json()


def convert_document(input_path: str,
                     output_path: str = "",
                     output_format: str = "") -> str:
    """
    Convert a document between formats.

    Supported conversions:
      - Excel (.xlsx) → CSV
      - CSV → Excel (.xlsx)
      - Word (.docx) → Markdown text
      - PDF → Plain text

    Args:
        input_path:   Source file path.
        output_path:  Destination file path. If empty, auto-generates.
        output_format: Target format: 'csv', 'xlsx', 'markdown', 'txt'.
                       If empty, inferred from output_path extension.
    """
    src, err = _safe_path(input_path)
    if err:
        return ToolResult(success=False, content="", error=err).to_json()
    if not src or not src.is_file():
        return ToolResult(success=False, content="",
                          error=f"File not found: {input_path}").to_json()

    suffix = src.suffix.lower()
    fmt = output_format.lower() if output_format else ""

    # Determine output path
    if not output_path:
        fmt_map = {
            "csv": ".csv", "xlsx": ".xlsx", "markdown": ".md", "txt": ".txt",
        }
        ext = fmt_map.get(fmt, "")
        if not ext:
            return ToolResult(success=False, content="",
                              error="Must specify output_path or "
                                    "output_format.").to_json()
        output_path = str(src.with_suffix(ext))

    dst = Path(output_path).resolve()

    try:
        # Excel → CSV
        if suffix in (".xlsx", ".xls") and fmt in ("csv", "") and \
                dst.suffix.lower() == ".csv":
            err = _check_dep("openpyxl", "pip install 'phoenix-agent[doc]'")
            if err:
                return err
            import pandas as pd
            xls = pd.ExcelFile(str(src))
            parts = []
            for sheet in xls.sheet_names:
                df = pd.read_excel(xls, sheet_name=sheet)
                csv_path = dst if len(xls.sheet_names) == 1 \
                    else dst.with_stem(f"{dst.stem}_{sheet}")
                df.to_csv(str(csv_path), index=False, encoding="utf-8-sig")
                parts.append(f"Sheet '{sheet}' → {csv_path} ({len(df)} rows)")
            return ToolResult(success=True,
                              content="Converted:\n" + "\n".join(parts),
                              metadata={"output": output_path}).to_json()

        # CSV → Excel
        if suffix == ".csv" and fmt in ("xlsx", "") and \
                dst.suffix.lower() == ".xlsx":
            err = _check_dep("openpyxl", "pip install 'phoenix-agent[doc]'")
            if err:
                return err
            import pandas as pd
            df = pd.read_csv(str(src))
            df.to_excel(str(dst), index=False)
            return ToolResult(
                success=True,
                content=f"Converted: {src.name} → {dst.name} "
                        f"({len(df)} rows, {len(df.columns)} columns)",
                metadata={"output": output_path},
            ).to_json()

        # Word → Markdown
        if suffix == ".docx" and fmt in ("markdown", "md", "txt", ""):
            err = _check_dep("docx", "pip install 'phoenix-agent[doc]'")
            if err:
                return err
            from docx import Document
            doc = Document(str(src))
            md_lines = []
            for para in doc.paragraphs:
                style = para.style.name if para.style else ""
                text = para.text.strip()
                if not text:
                    continue
                if style and "Heading" in style:
                    level = style.replace("Heading ", "").replace("heading", "")
                    try:
                        lvl = int(level)
                        md_lines.append("#" * min(lvl, 6) + " " + text)
                    except ValueError:
                        md_lines.append("## " + text)
                else:
                    md_lines.append(text)
            dst.write_text("\n\n".join(md_lines), encoding="utf-8")
            return ToolResult(
                success=True,
                content=f"Converted: {src.name} → {dst.name} "
                        f"({len(md_lines)} paragraphs)",
                metadata={"output": output_path},
            ).to_json()

        # PDF → Text
        if suffix == ".pdf" and fmt in ("txt", "text", ""):
            err = _check_dep("pdfplumber", "pip install 'phoenix-agent[doc]'")
            if err:
                return err
            import pdfplumber
            text_parts = []
            with pdfplumber.open(str(src)) as pdf:
                for page in pdf.pages:
                    t = page.extract_text()
                    if t:
                        text_parts.append(t)
            dst.write_text("\n\n".join(text_parts), encoding="utf-8")
            return ToolResult(
                success=True,
                content=f"Converted: {src.name} → {dst.name} "
                        f"({len(pdf.pages)} pages)",
                metadata={"output": output_path},
            ).to_json()

        return ToolResult(
            success=False, content="",
            error=f"Unsupported conversion: {suffix} → {fmt or dst.suffix}. "
                  f"Supported: xlsx→csv, csv→xlsx, docx→markdown, pdf→txt.",
        ).to_json()
    except Exception as exc:
        return ToolResult(success=False, content="",
                          error=f"Conversion error: {exc}").to_json()


def image_info(file_path: str) -> str:
    """
    Get detailed information about an image file: dimensions, format,
    color mode, file size, and optionally EXIF data.

    Args:
        file_path: Path to the image file.
    """
    err = _check_dep("PIL", "pip install 'phoenix-agent[doc]'")
    if err:
        return err

    path, perr = _safe_path(file_path)
    if perr:
        return ToolResult(success=False, content="", error=perr).to_json()
    if not path or not path.is_file():
        return ToolResult(success=False, content="",
                          error=f"File not found: {file_path}").to_json()

    try:
        from PIL import Image
        img = Image.open(str(path))
        size = path.stat().st_size

        info_lines = [
            f"Image: {path.name}",
            f"Format: {img.format} ({img.mode})",
            f"Dimensions: {img.width} x {img.height} px",
            f"File size: {size:,} bytes ({size / 1024:.1f} KB)",
        ]

        # EXIF
        if hasattr(img, "_getexif") and img._getexif():
            exif = img._getexif()
            from PIL import ExifTags
            info_lines.append("\nEXIF Data:")
            for tag_id, value in list(exif.items())[:20]:
                tag = ExifTags.TAGS.get(tag_id, tag_id)
                if isinstance(value, bytes):
                    try:
                        value = value.decode("utf-8", errors="replace")
                    except Exception:
                        value = f"<{len(value)} bytes>"
                info_lines.append(f"  {tag}: {value}")

        # Color palette summary
        if img.mode == "P":
            info_lines.append(f"\nPalette: {len(img.getpalette()) // 3} colors")

        # Animation frames
        if getattr(img, "is_animated", False):
            info_lines.append(f"Animated: {getattr(img, 'n_frames', '?')} frames")

        return ToolResult(
            success=True,
            content="\n".join(info_lines),
            metadata={
                "format": img.format,
                "mode": img.mode,
                "width": img.width,
                "height": img.height,
                "size": size,
            },
        ).to_json()
    except Exception as exc:
        return ToolResult(success=False, content="",
                          error=f"Image info error: {exc}").to_json()


# Auto-register tools when this module is loaded by the Skill system
_registry = ToolRegistry.get_instance()

for _func in [analyze_excel, convert_document, image_info]:
    _name = _func.__name__
    _desc = (_func.__doc__ or f"Document tool: {_name}")
    _desc = _desc.strip().split("\n")[0]
    _tool_def = ToolDefinition(
        name=f"document-processor.{_name}",
        description=_desc,
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
        category=ToolCategory.UTILITY,
        enabled_by_default=False,
        handler=_func,
    )
    # Build proper schema from function signature
    import inspect
    sig = inspect.signature(_func)
    params = {}
    type_map = {
        str: "string", int: "integer", float: "number",
        bool: "boolean", list: "array", dict: "object",
    }
    hints = {}
    try:
        hints = _func.__annotations__
    except Exception:
        pass
    required = []
    for pname, param in sig.parameters.items():
        if pname == "self":
            continue
        ptype = type_map.get(hints.get(pname, str), "string")
        params[pname] = {"type": ptype, "description": f"Parameter {pname}"}
        if param.default is inspect.Parameter.empty:
            required.append(pname)
    _tool_def.parameters = {
        "type": "object",
        "properties": params,
        "required": required,
    }
    ToolRegistry.register(_tool_def)
