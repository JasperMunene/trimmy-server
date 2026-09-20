from sqlalchemy.types import TypeDecorator, CHAR
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
import uuid as uuid_pkg


class UUID(TypeDecorator):
    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_UUID())
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return value
        if not isinstance(value, uuid_pkg.UUID):
            try:
                value = uuid_pkg.UUID(value)
            except (ValueError, AttributeError):
                raise ValueError(f"Cannot convert {value} to UUID")
        return str(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return value
        if isinstance(value, uuid_pkg.UUID):
            return value
        return uuid_pkg.UUID(value)


def uuid4() -> uuid_pkg.UUID:
    return uuid_pkg.uuid4()
