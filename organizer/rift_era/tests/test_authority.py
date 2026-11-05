import unittest
from types import SimpleNamespace as NS
from organizer.rift_era.engine.authority import Authority
from organizer.rift_era.engine.determinism import fingerprint
from organizer.rift_era.engine.errors import RuleViolation, InvariantViolation
from organizer.rift_era.engine.numeric import Q


class AuthorityTests(unittest.TestCase):
    def book(self):
        world = NS(regions={'r': NS(sovereign='B')}, hubs={'h': NS(region='r')})
        return Authority(world, {'enemy': NS(owner='B', hub='h'), 'neutral': NS(owner='C', hub='h')}, {'A','B','C'})

    def test_occupation_changes_control_after_ten_ticks_not_property(self):
        book = self.book()
        before = fingerprint(book.assets)
        for tick in range(9):
            self.assertFalse(book.advance_control(tick, sole_garrisons={'r':'A'}, hostile_defenders={}))
            self.assertEqual(book.regions['r'].controller, 'B')
        changes = book.advance_control(9, sole_garrisons={'r':'A'}, hostile_defenders={})
        self.assertEqual(changes, (('r','B','A'),))
        self.assertEqual(book.regions['r'].sovereign, 'B')
        self.assertEqual(book.regions['r'].support_mU, 280)
        self.assertEqual(before, fingerprint(book.assets))
        war = lambda a,b: {a,b} == {'A','B'}
        self.assertEqual(book.military_operating_factor('enemy', war), Q(3,5))
        self.assertEqual(book.military_operating_factor('neutral', war), 1)
        book.audit()

    def test_contested_garrison_breaks_streak_and_recapture_sets_550(self):
        book = self.book()
        for tick in range(9):
            book.advance_control(tick, sole_garrisons={'r':'A'}, hostile_defenders={})
        book.advance_control(9, sole_garrisons={'r':'A'}, hostile_defenders={'r':True})
        self.assertEqual(book.regions['r'].garrison_ticks, 0)
        for tick in range(10,20):
            book.advance_control(tick, sole_garrisons={'r':'A'}, hostile_defenders={})
        for tick in range(20,30):
            book.advance_control(tick, sole_garrisons={'r':'B'}, hostile_defenders={})
        self.assertEqual(book.regions['r'].controller, 'B')
        self.assertEqual(book.regions['r'].support_mU, 550)
        with self.assertRaises(InvariantViolation):
            book.advance_control(31, sole_garrisons={}, hostile_defenders={})

    def test_collateral_cannot_be_double_pledged_or_sold(self):
        book = self.book()
        book.pledge('neutral','loan','C',600)
        before = fingerprint(book.snapshot())
        with self.assertRaises(RuleViolation):
            book.pledge('neutral','other','C',500)
        with self.assertRaises(RuleViolation):
            book.transfer_shares('neutral','C','A',500)
        self.assertEqual(before, fingerprint(book.snapshot()))
        book.transfer_shares('neutral','C','A',200,collateral_id='loan')
        self.assertEqual(book.assets['neutral'].owner_shares, {'C':800,'A':200})
        self.assertEqual(book.assets['neutral'].pledged, {'loan':{'C':400}})
        self.assertEqual(book.assets['neutral'].operator, 'C')
        self.assertEqual(book.regions['r'].controller, 'B')
        book.audit()

    def test_cession_is_atomic_and_does_not_rewrite_private_property(self):
        book = self.book()
        before = fingerprint(book.snapshot())
        with self.assertRaises(RuleViolation):
            book.cede('B','A',('r','absent'))
        self.assertEqual(before, fingerprint(book.snapshot()))
        book.cede('B','A',('r',))
        self.assertEqual(book.regions['r'].sovereign, 'A')
        self.assertEqual(book.regions['r'].controller, 'B')
        self.assertEqual(book.assets['neutral'].owner_shares, {'C':1000})
        book.sync_assets({'enemy': NS(owner='A',hub='h'), 'neutral': NS(owner='A',hub='h')})
        self.assertEqual(book.assets['neutral'].owner_shares, {'C':1000})
        book.audit()
