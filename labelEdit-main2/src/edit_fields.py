"""İşlem sayfasındaki alanları kaynak sütun adlarıyla eşleştirir."""
from excel_schema import normalize_header


FIELD_NAMES = {
    "brand": ("Marka", "brand", "brand name", "marka adi"),
    "main_category": ("Ana Kategori", "main category", "category main", "kategori", "category"),
    "sub_category": ("Alt Kategori", "sub category", "subcategory", "category sub"),
    "poi_state": ("POI State", "Pol State", "poi status", "isletme durumu", "business status"),
}
EDITABLE_FIELDS = {"brand", "main_category", "sub_category"}


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def field_columns(columns):
    normalized = {normalize_header(c): c for c in columns}
    return {
        key: next((normalized[normalize_header(alias)] for alias in aliases
                   if normalize_header(alias) in normalized), None)
        for key, aliases in FIELD_NAMES.items()
    }


def read_fields(conn, table, index_column, columns, row_index):
    row = conn.execute(
        f"SELECT * FROM {quote(table)} WHERE {quote(index_column)}=?", (row_index,)
    ).fetchone()
    if row is None:
        raise KeyError("Kayıt bulunamadı.")
    mapping = field_columns(columns)
    return {key: str(row[col]) if col and row[col] is not None else ""
            for key, col in mapping.items()}


def read_options(conn, table, columns):
    result = {}
    for key, col in field_columns(columns).items():
        if key in EDITABLE_FIELDS:
            result[key] = [str(row[0]) for row in conn.execute(
                f"SELECT DISTINCT {quote(col)} FROM {quote(table)} "
                f"WHERE TRIM(COALESCE({quote(col)}, ''))<>'' ORDER BY {quote(col)}"
            )] if col else []
    return result


def validate_fields(fields):
    if set(fields) - EDITABLE_FIELDS:
        raise ValueError("Geçersiz işlem sayfası alanı.")


def write_fields(conn, table, index_column, columns, row_index, fields):
    """Çağıranın transaction'ı içinde kaydeder; eklenen sütunları döndürür."""
    validate_fields(fields)
    read_fields(conn, table, index_column, columns, row_index)
    mapping = field_columns(columns)
    added = []
    for key, value in fields.items():
        value = value.strip()
        col = mapping[key]
        if col is None:
            if not value:
                continue
            col = FIELD_NAMES[key][0]
            conn.execute(f"ALTER TABLE {quote(table)} ADD COLUMN {quote(col)} TEXT")
            added.append(col)
        conn.execute(
            f"UPDATE {quote(table)} SET {quote(col)}=? WHERE {quote(index_column)}=?",
            (value, row_index),
        )
    return added
