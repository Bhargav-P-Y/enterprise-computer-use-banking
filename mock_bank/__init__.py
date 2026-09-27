"""Mock bank package for legacy back-office core simulation."""

from mock_bank.database import db
from mock_bank.server import app, run_server

__all__ = ["db", "app", "run_server"]
