# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""enforce_wallet_balance and the quantity-correction flag (IDEV-3266).

fuelbuddy_crm's amend_delivery_note sets ``doc.flags.fb_qc_amend`` on the Delivery Note it saves
or reissues. The wallet must not refuse that note or raise its own block Issue for it, but the
recompute hooks still book it, so the wallet can go below zero.

Pure: frappe is stubbed, no site needed. From the app directory:

    python -m unittest fuelbuddy_wallet.tests.test_qc_amend_bypass

The stub is visible only while wallet.py is imported under its own module name, so this also
runs beside the real frappe under ``bench run-tests``.
"""

import datetime
import importlib.util
import pathlib
import sys
import types
import unittest
from unittest import mock

WALLET_PY = pathlib.Path(__file__).parents[1] / "fuelbuddy_wallet" / "doctype" / "wallet" / "wallet.py"
NOW = datetime.datetime(2026, 9, 30, 12, 0, 0)
WALLET = "WAL-0001"


class _Dict(dict):
	"""frappe._dict: attribute access; a missing key reads as None."""

	def __getattr__(self, key):
		return self.get(key)

	def __setattr__(self, key, value):
		self[key] = value


class Thrown(Exception):
	"""What the stubbed frappe.throw raises."""


def _stub_frappe():
	frappe = types.ModuleType("frappe")
	frappe._dict = _Dict
	frappe.db = mock.MagicMock(name="frappe.db")
	frappe.enqueue = mock.MagicMock(name="frappe.enqueue")

	def throw(msg, exc=None, title=None, **kwargs):
		raise Thrown(msg)

	frappe.throw = throw
	frappe.whitelist = lambda *args, **kwargs: lambda fn: fn

	model = types.ModuleType("frappe.model")
	document = types.ModuleType("frappe.model.document")
	document.Document = type("Document", (), {})
	utils = types.ModuleType("frappe.utils")
	utils.flt = lambda value, precision=None: (
		round(float(value or 0), precision) if precision is not None else float(value or 0)
	)
	utils.cint = lambda value: int(value or 0)
	utils.getdate = lambda value: (
		value if isinstance(value, datetime.date) else datetime.date.fromisoformat(str(value)[:10])
	)
	utils.now_datetime = lambda: NOW
	utils.add_to_date = lambda date, hours=0, **kwargs: date + datetime.timedelta(hours=hours)
	utils.time_diff_in_seconds = lambda a, b: (a - b).total_seconds()
	frappe.model, model.document, frappe.utils = model, document, utils
	modules = {"frappe": frappe, "frappe.model": model, "frappe.model.document": document, "frappe.utils": utils}
	return frappe, modules


def _load_wallet():
	"""A fresh wallet.py bound to a fresh stub frappe."""
	frappe, modules = _stub_frappe()
	with mock.patch.dict(sys.modules, modules):
		spec = importlib.util.spec_from_file_location("_fuelbuddy_wallet_under_test", WALLET_PY)
		wallet = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(wallet)
	return frappe, wallet


class DeliveryNote:
	def __init__(self, flags=None, **fields):
		self.name = "MAT-DN-2026-00001"
		self.customer = "CUST-QC"
		self.posting_date = "2026-09-15"
		self.grand_total = 0.0
		self.flags = _Dict(flags or {})
		self.__dict__.update(fields)

	def get(self, key, default=None):
		return getattr(self, key, default)


class TestEnforceWalletBalanceQcAmend(unittest.TestCase):
	"""Wallet: 100 received. The valuation helpers are stubbed; the numbers are set per test."""

	def setUp(self):
		self.frappe, self.wallet = _load_wallet()
		self.row = _Dict(
			name=WALLET,
			amount_received=100.0,
			amount_remaining=100.0,
			enable_breach=0,
			breach_amount=0.0,
			date_of_breach=None,
			wallet_start_date=None,
		)
		self.committed = self.other_drafts = self.dn_amount = 0.0
		self.frappe.db.get_single_value.return_value = 1  # Fuelbuddy Settings.enable_wallet
		self.frappe.db.get_value.side_effect = self._get_value
		patcher = mock.patch.multiple(self.wallet, _tracked_dn_value=mock.DEFAULT, _doc_dn_value=mock.DEFAULT)
		stubs = patcher.start()
		self.addCleanup(patcher.stop)
		stubs["_tracked_dn_value"].side_effect = self._tracked
		stubs["_doc_dn_value"].side_effect = lambda doc: self.dn_amount

	def _get_value(self, doctype, filters=None, fieldname=None, *args, **kwargs):
		if doctype == "Wallet":
			return self.row
		if doctype == "Issue":
			return None  # no open block ticket
		raise AssertionError(f"unexpected get_value on {doctype}")

	def _tracked(self, customer, start_date, docstatus, exclude=None):
		return self.committed if tuple(docstatus) == (1,) else self.other_drafts

	def enforce(self, **flags):
		return self.wallet.enforce_wallet_balance(DeliveryNote(flags=flags))

	def wallet_writes(self):
		return [c.args for c in self.frappe.db.set_value.call_args_list]

	def test_flag_name_is_the_one_crm_sets(self):
		self.assertEqual(self.wallet.QC_AMEND_FLAG, "fb_qc_amend")

	def test_shortfall_without_the_flag_is_refused_with_a_block_issue(self):
		self.dn_amount = 150.0
		with self.assertRaises(Thrown):
			self.enforce()
		self.frappe.enqueue.assert_called_once()
		self.assertEqual(self.frappe.enqueue.call_args.kwargs["doc"]["doctype"], "Issue")

	def test_shortfall_with_the_flag_passes_without_a_block_issue(self):
		self.dn_amount = 150.0
		self.assertIsNone(self.enforce(fb_qc_amend=True))
		self.frappe.enqueue.assert_not_called()
		self.assertEqual(self.wallet_writes(), [])

	def test_the_flag_passes_a_shortfall_made_by_other_notes(self):
		self.committed, self.other_drafts, self.dn_amount = 60.0, 30.0, 20.0
		with self.assertRaises(Thrown):
			self.enforce()
		self.frappe.enqueue.reset_mock()
		self.assertIsNone(self.enforce(fb_qc_amend=True))
		self.frappe.enqueue.assert_not_called()

	def test_a_falsy_flag_does_not_bypass(self):
		self.dn_amount = 150.0
		with self.assertRaises(Thrown):
			self.enforce(fb_qc_amend=False)

	def test_the_flag_covers_only_its_own_document(self):
		self.dn_amount = 150.0
		self.enforce(fb_qc_amend=True)
		with self.assertRaises(Thrown):
			self.enforce()

	def test_within_balance_nothing_is_written_either_way(self):
		self.dn_amount = 40.0
		for flags in ({}, {"fb_qc_amend": True}):
			with self.subTest(flags=flags):
				self.assertIsNone(self.enforce(**flags))
		self.frappe.enqueue.assert_not_called()
		self.assertEqual(self.wallet_writes(), [])

	def test_a_breach_allowance_is_used_as_for_any_delivery_note(self):
		self.row.update(enable_breach=1, breach_amount=100.0)
		self.dn_amount = 150.0
		self.assertIsNone(self.enforce(fb_qc_amend=True))
		self.assertEqual(self.wallet_writes(), [("Wallet", WALLET, "date_of_breach", NOW)])

	def test_an_expired_breach_is_still_closed(self):
		self.row.update(enable_breach=1, breach_amount=100.0, date_of_breach=NOW - datetime.timedelta(hours=13))
		self.dn_amount = 150.0
		self.assertIsNone(self.enforce(fb_qc_amend=True))
		self.assertEqual(self.wallet_writes(), [("Wallet", WALLET, {"enable_breach": 0, "breach_amount": 0})])
		self.frappe.enqueue.assert_not_called()

	def test_wallet_switched_off_never_blocks(self):
		self.frappe.db.get_single_value.return_value = 0
		self.dn_amount = 150.0
		self.assertIsNone(self.enforce())


class TestRecomputeIgnoresTheFlag(unittest.TestCase):
	"""The adjustment is the recompute hooks' job: a flagged note is booked, even below zero."""

	def setUp(self):
		self.frappe, self.wallet = _load_wallet()
		self.frappe.db.get_single_value.return_value = 1
		self.frappe.db.get_value.return_value = WALLET  # get_customer_wallet
		# Received comes from the customer GL here; a later wallet version derives it from
		# Payment Entries (customer_payments_total). Stub whichever this wallet.py has.
		received = next(
			name for name in ("customer_gl_balance", "customer_payments_total") if hasattr(self.wallet, name)
		)
		patcher = mock.patch.multiple(
			self.wallet, **{received: mock.DEFAULT, "customer_delivered_total": mock.DEFAULT}
		)
		stubs = patcher.start()
		self.addCleanup(patcher.stop)
		stubs[received].return_value = 100.0
		stubs["customer_delivered_total"].return_value = 150.0

	def test_update_cancel_and_delete_hooks_book_the_correction_below_zero(self):
		hooks = (self.wallet.update_wallet_on_delivery_note, self.wallet.update_wallet_on_delivery_note_cancel)
		for hook in hooks:
			with self.subTest(hook.__name__):
				self.frappe.db.set_value.reset_mock()
				hook(DeliveryNote(flags={"fb_qc_amend": True}))
				self.frappe.db.set_value.assert_called_once_with(
					"Wallet",
					WALLET,
					{"amount_received": 100.0, "amount_delivered": 150.0, "amount_remaining": -50.0},
				)


if __name__ == "__main__":
	unittest.main()
