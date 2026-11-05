import unittest
from organizer.rift_era.engine.observation import Observer,Sighting
from organizer.rift_era.engine.numeric import Q
from organizer.rift_era.engine.determinism import fingerprint
from organizer.rift_era.engine.errors import InfrastructureFault


class ObservationAgingTests(unittest.TestCase):
    def test_frozen_domain_drives_quality_radius_and_width_without_query_mutation(self):
        observer=Observer('A')
        observer.update(0,(Sighting('hidden',0,'visual','r',600,940,1060,'Land',owner='B',estimated_center_mU=1000),))
        observer.update(1,())
        before=fingerprint(observer.state_snapshot())
        for _ in range(100):
            view=observer.view(10,{'Land':Q(69,20)})['tracks'][0]
            self.assertEqual(view['quality_mU'],134)
            self.assertEqual(view['position_radius_hops'],4)
            self.assertEqual((view['estimated_low_mU'],view['estimated_high_mU']),(740,1260))
        self.assertEqual(before,fingerprint(observer.state_snapshot()))
        self.assertEqual(observer.expire(31,{'Land':Q(69,20)}),())
        self.assertEqual(observer.expire(32,{'Land':Q(69,20)}),('contact-1',))

    def test_visible_contact_does_not_age_and_hidden_death_never_becomes_deletion(self):
        observer=Observer('A')
        observer.update(0,(Sighting('unit',0,'visual','r',600,940,1060,'Land',estimated_center_mU=1000),))
        self.assertEqual(observer.view(5,{'Land':Q(69,20)})['tracks'][0]['quality_mU'],600)
        result=observer.update(5,())
        self.assertEqual(result['lost_contact_ids'],['contact-1'])
        result=observer.update(10,())
        self.assertFalse(result['deleted_ids'])
        self.assertFalse(observer.view(10,{'Land':Q(69,20)})['tracks'][0]['currently_visible'])

    def test_domain_cannot_change_under_a_frozen_trajectory_identity(self):
        observer=Observer('A')
        observer.update(0,(Sighting('unit',0,'visual','r',600,1,2,'Land'),))
        with self.assertRaises(InfrastructureFault):
            observer.update(1,(Sighting('unit',1,'visual','r',600,1,2,'Air'),))
