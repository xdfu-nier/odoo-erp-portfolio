import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from verify_native import validate_navigation  # noqa: E402


class NavigationVerificationTests(unittest.TestCase):
    def test_action_metadata_alone_does_not_prove_navigation_exists(self):
        with self.assertRaises(ValueError):
            validate_navigation([], [], [], {"sale.order"})

    def test_missing_menu_or_wrong_action_and_view_mode_are_rejected(self):
        models = ["sale.order", "purchase.order", "stock.picking", "account.move"]
        names = ["sales", "purchases", "stock", "invoices"]
        refs = [{"name": "menu_portfolio_root", "res_id": 1}]
        actions = []
        menus = [{"id": 1, "parent_id": False, "action": False}]
        for index, (name, model) in enumerate(zip(names, models), start=2):
            refs.extend([
                {"name": f"menu_portfolio_{name}", "res_id": index},
                {"name": f"action_portfolio_{name}", "res_id": index + 10},
            ])
            actions.append({"id": index + 10, "res_model": model, "view_mode": "list,form"})
            menus.append({"id": index, "parent_id": [1, "Root"],
                          "action": f"ir.actions.act_window,{index + 10}"})
        self.assertEqual(validate_navigation(refs, actions, menus, set(models)), sorted(models))
        for changed in (menus[:-1], [dict(menu, action=False) for menu in menus]):
            with self.subTest(menus=changed), self.assertRaises(ValueError):
                validate_navigation(refs, actions, changed, set(models))
        actions[0]["view_mode"] = "form"
        with self.assertRaises(ValueError):
            validate_navigation(refs, actions, menus, set(models))
