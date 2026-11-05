import unittest
from organizer.rift_era.engine.march import Position,trajectory,first_meeting,simultaneous_trajectories
from organizer.rift_era.engine.numeric import Q


class MarchTests(unittest.TestCase):
    def test_partial_edge_then_new_edge_uses_only_remaining_tick(self):
        route=trajectory(Position('a','b',Q(900)),('b','c'),Q(500),lambda a,b:Q(1) if b=='b' else Q(2))
        self.assertEqual(route[0].end,Q(1,5))
        self.assertEqual(route[-1].after,Position('b','c',Q(200)))

    def test_opposing_armies_stop_at_exact_intersection_and_third_party_does_not(self):
        paths={'A':trajectory(Position('a','b',Q(400)),('b',),Q(300),lambda a,b:Q(1)),
               'B':trajectory(Position('b','a',Q(400)),('a',),Q(100),lambda a,b:Q(1)),
               'C':trajectory(Position('a','b',Q(400)),('b',),Q(300),lambda a,b:Q(1))}
        stopped,events=simultaneous_trajectories(paths,lambda a,b:{a,b}=={'A','B'})
        self.assertEqual(events[0][2],Q(1,2))
        self.assertEqual(stopped['A'][-1].after,Position('a','b',Q(550)))
        self.assertEqual(stopped['B'][-1].after,Position('b','a',Q(450)))
        self.assertEqual(stopped['C'][-1].after,Position('a','b',Q(700)))
        reverse=simultaneous_trajectories(dict(reversed(tuple(paths.items()))),lambda a,b:{a,b}=={'A','B'})
        self.assertEqual((stopped,events),reverse)

    def test_stationary_endpoint_blocks_arrival_without_consuming_extra_distance(self):
        left=trajectory(Position('a','b',Q(900)),('b','c'),Q(500),lambda a,b:Q(1))
        right=trajectory(Position('b'),(),Q(0),lambda a,b:Q(1))
        self.assertEqual(first_meeting(left,right),(Q(1,5),Position('b')))
        result,events=simultaneous_trajectories({'a':left,'b':right},lambda a,b:True)
        self.assertEqual(result['a'][-1].after,Position('b'))
        self.assertEqual(len(events),1)

    def test_distinct_edges_meet_at_their_shared_endpoint(self):
        left=trajectory(Position('a','c',Q(900)),('c',),Q(100),lambda a,b:Q(1))
        right=trajectory(Position('b','c',Q(800)),('c',),Q(200),lambda a,b:Q(1))
        self.assertEqual(first_meeting(left,right),(Q(1),Position('c')))

    def test_parallel_corridors_do_not_create_false_crossing_combat(self):
        a=trajectory(Position('a','b',Q(400),'road'),('b',),Q(300),lambda a,b:Q(1),edge_ids=('road',))
        b=trajectory(Position('b','a',Q(400),'rail'),('a',),Q(300),lambda a,b:Q(1),edge_ids=('rail',))
        self.assertIsNone(first_meeting(a,b))

    def test_region_waypoint_stops_partway_along_real_corridor(self):
        route=trajectory(Position('a'),('b',),Q(500),lambda a,b:Q(1),edge_ids=('road',),end_progress=Q(250))
        self.assertEqual(route[0].end,Q(1,2))
        self.assertEqual(route[-1].after,Position('a','b',Q(250),'road'))

    def test_l1_terrain_runs_spend_the_same_tick_time_budget(self):
        route=trajectory(Position('a'),('b',),Q(500),lambda a,b:Q(1),
                         profiles=(((Q(100),Q(1)),(Q(1000),Q(5,2))),))
        self.assertEqual(route[0].end,Q(1,5))
        self.assertEqual(route[-1].after.progress,Q(260))
        self.assertEqual(route[-1].end,Q(1))

    def test_already_colocated_army_can_disengage(self):
        departing=trajectory(Position('a'),('b',),Q(300),lambda a,b:Q(1),edge_ids=('road',))
        standing=trajectory(Position('a'),(),Q(0),lambda a,b:Q(1))
        self.assertIsNone(first_meeting(departing,standing))

    def test_separating_then_returning_is_a_new_encounter(self):
        turning=trajectory(Position('a','b',Q(900),'road'),('b','a'),Q(300),lambda a,b:Q(1),
                           edge_ids=('road','road'),end_progress=Q(200))
        standing=trajectory(Position('a','b',Q(900),'road'),('b',),Q(0),lambda a,b:Q(1))
        meeting=first_meeting(turning,standing)
        self.assertEqual(meeting[0],Q(2,3))
