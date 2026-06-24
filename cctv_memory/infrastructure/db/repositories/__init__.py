"""SQLite repository adapters.

Concrete implementations of repository ports. They keep SQLAlchemy
sessions/ORM objects internal and return only contract/domain DTOs
(database-adapter-contract §2, repository-port-contract §0).
"""
