import unittest
from organizer.rift_era.engine.capital_book import CAPITAL_BOOK_SCALE,proportional_loss,loss_error_bound_nmU
from organizer.rift_era.engine.determinism import canonical
from organizer.rift_era.engine.numeric import Q


class CapitalBookTests(unittest.TestCase):
    def test_repeated_damage_and_repair_has_bounded_state_and_conserves_cost(self):
        value=oracle=Q(1000000);loss=Q(0);steps=2000
        for step in range(steps):
            removed=proportional_loss({'steel':value},1,997)['steel']
            value-=removed;loss+=removed
            oracle*=Q(996,997)
            value+=1000;oracle+=1000
            self.assertGreaterEqual(value,oracle)
            self.assertLessEqual(value-oracle,loss_error_bound_nmU(step+1))
            self.assertEqual(CAPITAL_BOOK_SCALE%value.denominator,0)
        self.assertEqual(value+loss,1000000+1000*steps)
        self.assertGreater(oracle.denominator.bit_length(),15000)
        self.assertLess(len(canonical({'book_nmU':value})),120)

    def test_large_integer_cost_never_converts_to_float(self):
        source=10**400
        actual=proportional_loss({'steel':source},3,7)['steel']
        self.assertIsInstance(actual,Q)
        self.assertLessEqual(actual,Q(source*3,7))
        self.assertLess(Q(source*3,7)-actual,Q(1,CAPITAL_BOOK_SCALE))

    def test_total_destruction_removes_exact_remaining_cost(self):
        book={'steel':Q(123456789,CAPITAL_BOOK_SCALE)}
        self.assertEqual(proportional_loss(book,17,17),book)
        self.assertEqual(proportional_loss(book,0,0),{})
