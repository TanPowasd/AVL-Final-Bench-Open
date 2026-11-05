import unittest
from types import SimpleNamespace as NS

from organizer.rift_era.engine.assets import Asset
from organizer.rift_era.engine.catalog import Catalog
from organizer.rift_era.engine.clock import ClockConfig
from organizer.rift_era.engine.determinism import fingerprint
from organizer.rift_era.engine.economy import opening_economy
from organizer.rift_era.engine.initialization import initialize, Facility
from organizer.rift_era.engine.ledger import Account
from organizer.rift_era.engine.materials import Origin
from organizer.rift_era.engine.numeric import Q
from organizer.rift_era.engine.orbital_operations import OrbitalCommands
from organizer.rift_era.engine.public_systems import InstalledSystem
from organizer.rift_era.engine.world import generate_candidate


class OrbitalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.catalog,cls.world=Catalog(),generate_candidate(1000)

    def fixture(self, owners=('n0',)):
        game=opening_economy(self.catalog,initialize(self.catalog,self.world),total_ticks=30,debug=False,commerce=True)
        state=game.state
        for owner in owners:
            hub=f'{owner}-h0';host=f'test-launch-host:{owner}';slot=f'{host}/slot'
            # Explicit prebuilt launch site fixture. Construction costs and
            # exclusive installation are tested independently in system tests.
            state.initial.assets[host]=Asset(host,owner,hub,{})
            state.initial.assets[slot]=Asset(slot,owner,hub,{})
            state.initial.facilities[host]=Facility(host,owner,hub,'S3','public',(slot,))
            state.slot_use[slot]=f'system:{slot}'
            state.systems.instances[slot]=InstalledSystem(slot,owner,hub,host,'launch_site',online=True)
            state.research[owner].known.update({'N06','N09','N08','C08'})
            goods={'satellite':24000,'reusable_launcher':1000,'superalloy':12000,'fuel':200000,'pure_chemicals':48000}
            stock={Account(owner,hub,'available',g):q for g,q in goods.items()}
            state.initial.inventory.transform({},stock,reason='test_initial_launch_supply',
                origins={a:Origin(owner,hub) for a in stock})
        state.authority.sync_assets(state.initial.assets)
        return game

    @staticmethod
    def ctx(tick):return NS(seed=1000,tick=tick,cycle=tick//10,window=tick//5,clock=ClockConfig())

    @staticmethod
    def order(owner,seq=1,slot=0,reusable=False):
        return {'id':str(seq),'kind':'intelligence','op':'orbital_launch','hub_id':f'{owner}-h0',
                'payload':'satellite','orbit_plane':0,'slot':slot,'berth':0,'use_reusable':reusable}

    def test_contested_slot_is_order_independent_and_loser_keeps_inventory(self):
        def run(owners):
            state=self.fixture(('n0','n1')).state
            handler=OrbitalCommands(state)
            for owner in owners:handler.prepare(owner,self.order(owner),self.ctx(0)).commit()
            before={p:state.initial.inventory.balance(Account(p,f'{p}-h0','available','satellite')) for p in owners}
            state.orbit.reserve(self.ctx(0))
            winner=next(j.owner for j in state.orbit.launches.values() if j.site is not None)
            loser=next(p for p in owners if p!=winner)
            self.assertEqual(state.initial.inventory.balance(Account(loser,f'{loser}-h0','available','satellite')),before[loser])
            self.assertEqual(state.initial.inventory.balance(Account(winner,f'{winner}-h0','available','satellite')),before[winner]-1000)
            state.orbit.audit();state.initial.inventory.audit()
            return winner
        self.assertEqual(run(('n0','n1')),run(('n1','n0')))

    def test_real_twenty_tick_preparation_power_pause_cancel_and_lifetime(self):
        game=self.fixture();state=game.state
        game.step({'n0':[self.order('n0')]})
        job=next(iter(state.orbit.launches.values()))
        self.assertEqual(job.elapsed,1)
        for _ in range(18):game.step()
        self.assertFalse(state.orbit.satellites)
        game.step()
        satellite,=state.orbit.satellites.values()
        self.assertEqual(satellite.commissioned_tick,20)
        self.assertEqual(satellite.expires_tick,1820)
        self.assertFalse(state.command_streams['n0'].plans)
        state.audit()
        state.orbit.begin_tick(self.ctx(1819))
        self.assertTrue(state.orbit.satellites)
        state.orbit.begin_tick(self.ctx(1820))
        self.assertFalse(state.orbit.satellites)
        self.assertEqual(state.initial.inventory.balance(state.orbit.satellite_account(satellite)),0)
        state.audit()

    def test_power_pause_and_cancellation_preserve_payload_and_launcher_uses(self):
        state=self.fixture().state;handler=OrbitalCommands(state)
        order=self.order('n0',reusable=True)
        before=fingerprint(state.snapshot())
        prepared=handler.prepare('n0',order,self.ctx(0))
        self.assertEqual(before,fingerprint(state.snapshot()))
        prepared.commit();state.orbit.reserve(self.ctx(0))
        job,=state.orbit.launches.values()
        state.systems.instances[job.site].online=False
        for tick in range(25):state.orbit.advance(self.ctx(tick))
        self.assertEqual(job.elapsed,0)
        self.assertEqual(job.status,'PAUSED')
        state.orbit.cancel('n0','1').commit()
        launcher,=state.orbit.launchers.values()
        self.assertEqual(launcher.uses,0)
        self.assertIsNone(launcher.busy)
        self.assertEqual(state.initial.inventory.balance(Account('n0','n0-h0','available','satellite')),24000)
        state.audit()

    def test_reusable_launcher_is_retired_on_twentieth_actual_launch(self):
        state=self.fixture().state;handler=OrbitalCommands(state)
        for n in range(20):
            tick=n*20
            handler.prepare('n0',self.order('n0',seq=n+1,slot=n%8,reusable=True)|{'orbit_plane':n//8},self.ctx(tick)).commit()
            state.orbit.reserve(self.ctx(tick))
            job,=state.orbit.launches.values()
            self.assertIsNotNone(job.site)
            for k in range(20):state.orbit.advance(self.ctx(tick+k))
            self.assertEqual(len(state.orbit.satellites),n+1)
            if n<19:self.assertEqual(next(iter(state.orbit.launchers.values())).uses,n+1)
            if n==1:self.assertIsNone(state.orbit.coverage('n0'))
            if n==2:self.assertEqual(state.orbit.coverage('n0'),('sea',Q(4,5)))
            if n==7:
                state.research['n0'].known.add('N12')
                self.assertEqual(state.orbit.coverage('n0'),('land_sea',Q(9,10)))
                state.orbit.advance(self.ctx(tick+19))
                self.assertEqual(state.orbit.visible('n1')['capacity'],48)
            state.orbit.audit()
        self.assertFalse(state.orbit.launchers)
        self.assertEqual(state.initial.inventory.balance(Account('n0','n0-h0','available','superalloy')),2000)
        self.assertEqual(state.initial.inventory.balance(Account('n0','n0-h0','available','reusable_launcher')),0)
        state.audit()

    def test_new_berth_requires_research_and_accepted_id_is_canonical(self):
        game=self.fixture();state=game.state
        denied=self.order('n0')|{'berth':1}
        game.step({'n0':[denied]})
        self.assertEqual(state.command_streams['n0'].recent[-1].code,'ERR_TECH_LOCKED')
        state.research['n0'].known.add('N12')
        for _ in range(4):game.step()
        game.step({'n0':[self.order('n0',seq=2)|{'id':'0002','berth':1}]})
        self.assertTrue(state.orbit.expanded)
        job,=state.orbit.launches.values()
        self.assertEqual(job.command_id,'2')
        self.assertEqual(job.berth,(0,0,1))
        state.orbit.cancel('n0','2').commit()
        self.assertFalse(state.command_streams['n0'].plans)
        self.assertFalse(state.orbit.launches)
        state.audit()

    def test_maximum_length_command_keeps_public_entity_ids_addressable(self):
        state=self.fixture().state;command_id='9'*64
        OrbitalCommands(state).prepare('n0',self.order('n0')|{'id':command_id},self.ctx(0)).commit()
        state.orbit.reserve(self.ctx(0))
        job,=state.orbit.launches.values()
        self.assertLessEqual(len(job.id),64)
        for tick in range(20):state.orbit.advance(self.ctx(tick))
        satellite,=state.orbit.satellites.values()
        self.assertLessEqual(len(satellite.id),64)
        state.audit()
