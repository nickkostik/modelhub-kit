"""Local metadata/artifact fixtures only; never a real installer or agent call."""
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import tarfile
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import zipfile
from modelhub_kit import updater
from modelhub_kit.core import GuardError
from modelhub_kit.native_helpers import products, desktop
from modelhub_kit.native_helpers import macos_apps


def binary(system='Linux', architecture='x64'):
    data = bytearray(128)
    if system == 'Windows':
        data[:2] = b'MZ'; data[60:64] = (80).to_bytes(4, 'little'); data[80:84] = b'PE\0\0'
        data[84:86] = (0x8664 if architecture == 'x64' else 0xaa64).to_bytes(2, 'little')
    elif system == 'Darwin':
        data[:4] = b'\xcf\xfa\xed\xfe'; data[4:8] = (0x1000007 if architecture == 'x64' else 0x100000c).to_bytes(4, 'little')
    else:
        data[:6] = b'\x7fELF\x02\x01'; data[18:20] = (62 if architecture == 'x64' else 183).to_bytes(2, 'little')
    return bytes(data)


def bundle(files, zipped=False):
    stream = io.BytesIO()
    if zipped:
        with zipfile.ZipFile(stream, 'w') as archive:
            for name, value in files.items():
                row = zipfile.ZipInfo(name); row.external_attr = (0o100755 << 16)
                archive.writestr(row, value)
    else:
        with tarfile.open(fileobj=stream, mode='w:gz') as archive:
            for name, value in files.items():
                row = tarfile.TarInfo(name); row.size = len(value); row.mode = 0o755
                archive.addfile(row, io.BytesIO(value))
    return stream.getvalue()


class NativeProducts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / 'home'; self.home.mkdir()
        self.state = self.home / 'state'; self.state.mkdir()
        self.version = '3000.1.0'; self.blob = bundle({'bin/devin': binary()}); self.transfers = 0
        self.probes = 0
    def fetch(self, product, url, limit=1024**2):
        return json.dumps({'version': self.version, 'platforms': {'x86_64-unknown-linux': {
            'url': f'https://static.devin.ai/cli/{self.version}/devin-{self.version}-x86_64-unknown-linux.tar.gz',
            'sha256': hashlib.sha256(self.blob).hexdigest()}}})
    def transfer(self, product, url, destination):
        self.transfers += 1; destination.write_bytes(self.blob)
        return hashlib.sha256(self.blob).hexdigest(), len(self.blob)
    def runner(self, argv, **kwargs):
        self.probes += 1
        self.assertEqual(argv[-1], '--version')
        self.assertNotIn('XAI_API_KEY', kwargs['env'])
        return {'exit': 0, 'output': 'Devin ' + self.version, 'outputTruncated': False}
    def reconcile(self, **kwargs):
        options = dict(home=self.home, state=self.state, system='Linux', architecture='x86_64', fetch=self.fetch, transfer=self.transfer, runner=self.runner, inventory=lambda: [])
        options.update(kwargs)
        return products.reconcile('devin-cli', **options)
    def test_install_current_update_and_exact_old_release_retirement(self):
        first = self.reconcile(install_missing=True)
        self.assertEqual(first['status'], 'installed')
        receipt = products.installed_receipt('devin-cli', self.home, self.state)
        old = Path(receipt['release']); self.assertTrue(old.exists())
        self.assertEqual(self.reconcile()['status'], 'current')
        self.assertEqual(self.transfers, 1)
        self.version = '3000.1.1'; self.blob = bundle({'bin/devin': binary() + b'next'})
        changed = self.reconcile(); self.assertEqual(changed['status'], 'updated')
        self.assertFalse(old.exists()); self.assertEqual(self.transfers, 2)
        current = json.loads((self.state / 'tools/devin-cli.json').read_text())
        self.assertNotIn('retirement', current)
        self.assertEqual(len(list((self.state / 'native/devin-cli').iterdir())), 1)
    def test_proven_newer_installation_is_settled_without_download(self):
        self.reconcile(install_missing=True)
        self.version = '2999.99.99'
        result = self.reconcile()
        self.assertEqual(result['status'], 'newer-installation-preserved')
        self.assertEqual(self.transfers, 1)

    def test_current_run_retires_one_late_active_old_release_without_reinstall(self):
        self.reconcile(install_missing=True)
        old=products.installed_receipt('devin-cli',self.home,self.state)['release']
        self.version='3000.1.1';self.blob=bundle({'bin/devin':binary()+b'next'})
        snapshots=iter([[],[],[old+'/bin/devin']])
        self.assertEqual(self.reconcile(inventory=lambda:next(snapshots))['status'],'updated')
        self.assertTrue(Path(old).exists())
        self.assertEqual(self.reconcile(inventory=lambda:None)['status'],'queued-native-retirement')
        self.assertEqual(self.reconcile()['status'],'current')
        self.assertFalse(Path(old).exists());self.assertEqual(self.transfers,2)

    def test_missing_custom_busy_and_dry_run_never_download(self):
        self.assertEqual(self.reconcile()['status'], 'not-installed')
        self.assertEqual(self.reconcile(command='/custom/devin', install_missing=True)['status'], 'unmanaged-installation-preserved')
        self.assertEqual(self.reconcile(install_missing=True, dry_run=True)['status'], 'planned-native-install')
        self.assertEqual(self.reconcile(install_missing=True, processes=None, inventory=lambda: None)['status'], 'queued-active-or-unknown-processes')
        self.assertEqual(self.transfers, 0); self.assertEqual(self.probes, 0)
    def test_checksum_failure_never_publishes_or_probes(self):
        def damaged(product, url, destination):
            destination.write_bytes(b'damaged'); return 'a' * 64, 7
        result = self.reconcile(install_missing=True, transfer=damaged)
        self.assertEqual(result['reason'], 'native-artifact-checksum-mismatch')
        self.assertFalse((self.home / '.local/bin/devin').exists()); self.assertEqual(self.probes, 0)
        self.assertEqual(list((self.state / 'native/devin-cli').iterdir()), [])
    def test_custom_file_and_late_process_preserve_working_pointer(self):
        self.reconcile(install_missing=True)
        receipt = products.installed_receipt('devin-cli', self.home, self.state); original = (self.home / '.local/bin/devin').resolve()
        (Path(receipt['release']) / 'custom.txt').write_text('custom')
        self.version = '3000.1.1'
        self.assertEqual(self.reconcile()['status'], 'custom-native-files-preserved')
        self.assertEqual((self.home / '.local/bin/devin').resolve(), original)
        (Path(receipt['release']) / 'custom.txt').unlink()
        snapshots = iter([[], ['devin busy']])
        result = self.reconcile(inventory=lambda: next(snapshots))
        self.assertEqual(result['status'], 'queued-active-or-unknown-processes')
        self.assertEqual((self.home / '.local/bin/devin').resolve(), original)
    def test_archive_links_and_escape_are_rejected(self):
        for name in ('../outside', '/absolute', 'dir/../../outside'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                archive = Path(tmp) / 'download'; archive.write_bytes(bundle({name: b'x'})); target = Path(tmp) / 'release'; target.mkdir()
                with self.assertRaises(GuardError): products.unpack(archive, target, {'kind': 'tar'})
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / 'download'; value = io.BytesIO()
            with tarfile.open(fileobj=value, mode='w:gz') as tar:
                row = tarfile.TarInfo('bin/devin'); row.type = tarfile.SYMTYPE; row.linkname = '/outside'; tar.addfile(row)
            archive.write_bytes(value.getvalue()); target = Path(tmp) / 'release'; target.mkdir()
            with self.assertRaises(GuardError): products.unpack(archive, target, {'kind': 'tar'})
    def test_native_architecture_and_native_windows_contracts(self):
        for system in ('Linux', 'Darwin', 'Windows'):
            for arch in ('x64', 'arm64'):
                file = self.home / 'native'; file.write_bytes(binary(system, arch))
                self.assertTrue(products.native_architecture(file, system, arch))
                self.assertFalse(products.native_architecture(file, system, 'arm64' if arch == 'x64' else 'x64'))
        def fetch(product, url):
            return json.dumps({'version': '3000.1.0', 'platforms': {'aarch64-pc-windows': {'url': 'https://static.devin.ai/cli/3000.1.0/devin-3000.1.0-aarch64-pc-windows.zip', 'sha256': 'a'*64}}})
        self.assertEqual(products.discover('devin-cli', 'Windows', 'arm64', fetch)['kind'], 'zip')
    def test_muse_six_platform_checksums_and_cursor_no_fake_publisher_digest(self):
        for system, os_id in [('Linux','linux'), ('Darwin','macos'), ('Windows','windows')]:
            for machine, arch in [('x86_64','x86'), ('aarch64','aarch64')]:
                version = '1.2.3-R4000.1'; suffix = '.exe' if system == 'Windows' else ''
                manifest_url = f'https://lookaside.facebook.com/lookaside/muse/download/?channel=muse&version={version}&file=manifest.json'
                def fetch(product, url):
                    return json.dumps({'channel':'muse-stable','state':'public','version':version,'manifest_url':manifest_url} if '/channels/' in url else {'version':version,'checksum_algorithm':'sha256','artifacts':{arch+'_'+os_id:{'url': f'https://lookaside.facebook.com/lookaside/muse/download/?channel=muse&version={version}&file=muse-{arch}-{os_id}{suffix}','checksum':'b'*64,'size':128}}})
                self.assertEqual(products.discover('muse',system,machine,fetch)['integrity'], 'publisher-sha256')
        metadata = products.discover('cursor-cli','Windows','arm64',lambda *a:'https://downloads.cursor.com/lab/2026.10.10-abcdef0/')
        self.assertIsNone(metadata['sha256']); self.assertEqual(metadata['integrity'],'official-https-sha256-readback')
        self.assertTrue(metadata['url'].endswith('/windows/arm64/agent-cli-package.zip'))
        with self.assertRaises(GuardError): products.official_url('devin-cli','https://other.invalid/payload')
    def test_explicit_dispatch_unknown_manager_and_readonly_description(self):
        with patch.object(updater,'tools',return_value={'new':{'manager':'unknown','command':'new'}}), patch.object(updater.shutil,'which',return_value=None), patch.object(updater,'run') as called:
            self.assertEqual(updater.update({},self.state,selected=['new'])[0]['status'],'unsupported-native-manager'); called.assert_not_called()
        self.reconcile(install_missing=True)
        with patch.object(updater.shutil,'which',return_value=str(self.home/'.local/bin/devin')),patch.object(updater,'run') as called:
            result = updater.describe('devin-cli',{'manager':'native-products','command':'devin'},self.state,self.home)
            self.assertEqual(result['version'],self.version); self.assertTrue(result['owned']);called.assert_not_called()

    def test_existing_npm_description_requires_exact_official_bin_without_execution(self):
        root = self.home / 'global/lib/node_modules/@getpaseo/cli'
        native = root / 'dist/cli.js'; native.parent.mkdir(parents=True)
        native.write_text('// fixture'); native.chmod(0o755)
        metadata = root / 'package.json'
        value = {'name':'@getpaseo/cli','version':'1.0.0','bin':{'paseo':'dist/cli.js'}}
        metadata.write_text(json.dumps(value))
        launcher = self.home / 'paseo'; launcher.symlink_to(native)
        definition = {'package':'@getpaseo/cli','command':'paseo'}
        with patch.object(updater.shutil,'which',return_value=str(launcher)), patch.object(updater,'run',side_effect=AssertionError('agent launch')):
            result = updater.describe('paseo',definition,self.state,self.home)
            self.assertEqual(result['version'],'1.0.0')
            self.assertIsNone(result['owned'])
            self.assertEqual(result['status'],'native-npm-metadata-verified')
            value['bin']['paseo'] = 'dist/custom.js'; metadata.write_text(json.dumps(value))
            self.assertNotIn('version',updater.describe('paseo',definition,self.state,self.home))
            value['bin']['paseo'] = 'dist/cli.js'; value['name']='custom'; metadata.write_text(json.dumps(value))
            self.assertNotIn('version',updater.describe('paseo',definition,self.state,self.home))
            value['name']='@getpaseo/cli'; metadata.write_text(json.dumps(value)); metadata.chmod(0o666)
            self.assertNotIn('version',updater.describe('paseo',definition,self.state,self.home))

    def test_windows_three_terminal_products_install_then_remain_current(self):
        for product in ('devin-cli', 'cursor-cli', 'muse'):
            with self.subTest(product=product), tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary) / 'home'; home.mkdir(); state = home / 'state'; state.mkdir()
                version = {'devin-cli':'3000.1.0','cursor-cli':'2026.10.10-abcdef0','muse':'1.2.3-R4000.1'}[product]
                blob = binary('Windows') if product == 'muse' else bundle(
                    {'dist-package/node.exe':binary('Windows'),'dist-package/index.js':b'// cursor fixture'} if product == 'cursor-cli' else {'bin/devin.exe':binary('Windows')}, zipped=True)
                checksum = hashlib.sha256(blob).hexdigest()
                def fetch(name, url):
                    if name == 'cursor-cli': return 'https://downloads.cursor.com/lab/' + version + '/'
                    if name == 'devin-cli': return json.dumps({'version':version,'platforms':{'x86_64-pc-windows':{'url':f'https://static.devin.ai/cli/{version}/devin-{version}-x86_64-pc-windows.zip','sha256':checksum}}})
                    return json.dumps({'channel':'muse-stable','state':'public','version':version,'manifest_url':f'https://lookaside.facebook.com/lookaside/muse/download/?channel=muse&version={version}&file=manifest.json'} if '/channels/' in url else {'version':version,'checksum_algorithm':'sha256','artifacts':{'x86_windows':{'url':f'https://lookaside.facebook.com/lookaside/muse/download/?channel=muse&version={version}&file=muse-x86-windows.exe','checksum':checksum,'size':len(blob)}}})
                transferred = []
                def transfer(name,url,file): transferred.append(name);file.write_bytes(blob);return checksum,len(blob)
                options=dict(home=home,state=state,system='Windows',architecture='x86_64',fetch=fetch,transfer=transfer,runner=lambda *a,**k:{'exit':0,'output':version},inventory=lambda:[])
                with patch.dict(os.environ,{'LOCALAPPDATA':str(home/'AppData/Local')}):
                    installed=products.reconcile(product,install_missing=True,**options)
                    self.assertEqual(installed['status'],'installed',installed)
                    self.assertEqual(products.reconcile(product,**options)['status'],'current')
                    self.assertEqual(len(transferred),1)
                    receipt=products.installed_receipt(product,home,state,system='Windows')
                    self.assertEqual(receipt['version'],version)
                    launcher=Path(receipt['launcher']).read_text()
                    self.assertNotIn('setup',launcher);self.assertNotIn('start ',launcher)

    def test_unreceipted_custom_npm_bin_is_never_updated_or_adopted(self):
        prefix=self.home/'global';package=prefix/'lib/node_modules/@getpaseo/cli';package.mkdir(parents=True)
        entry=package/'bin.js';entry.write_text('// official bin')
        custom=package/'custom.js';custom.write_text('// user customization')
        metadata=package/'package.json';value={'name':'@getpaseo/cli','version':'1.0.0','bin':{'paseo':'bin.js'}}
        metadata.write_text(json.dumps(value));launcher=self.home/'paseo';launcher.symlink_to(custom)
        definition={'package':'@getpaseo/cli','command':'paseo'}
        def command(argv,**options):
            self.assertEqual(argv,['npm','prefix','--global'])
            return {'exit':0,'output':str(prefix)}
        with patch.object(updater,'tools',return_value={'paseo':definition}),patch.object(updater,'npm_command',return_value=['npm']),patch.object(updater.shutil,'which',return_value=str(launcher)),patch.object(updater,'run',side_effect=command),patch.object(updater,'http_json',side_effect=AssertionError('unowned update discovery')),patch.object(updater,'expose_installation',side_effect=AssertionError('custom launcher adoption')):
            result=updater.update({},self.state,selected=['paseo'])[0]
            self.assertEqual(result['status'],'unmanaged-installation-preserved')
            self.assertEqual(launcher.resolve(),custom);self.assertEqual(custom.read_text(),'// user customization')
            # Even an exact bin path cannot authorize another package's identity.
            launcher.unlink();launcher.symlink_to(entry);value['name']='custom-package';metadata.write_text(json.dumps(value))
            self.assertEqual(updater.update({},self.state,selected=['paseo'])[0]['status'],'unmanaged-installation-preserved')
        value['name']='@getpaseo/cli';metadata.write_text(json.dumps(value))
        def changed(argv,**options):
            if argv==['npm','prefix','--global']:return {'exit':0,'output':str(prefix)}
            if '-p' in argv:return {'exit':0,'output':json.dumps({'arch':'x64','platform':'linux'})}
            self.fail('Custom launcher reached an install transaction')
        def latest(url):
            launcher.unlink();launcher.symlink_to(custom)
            return {'version':'2.0.0'},{}
        with patch.object(updater,'tools',return_value={'paseo':definition}),patch.object(updater,'npm_command',return_value=['npm']),patch.object(updater.shutil,'which',return_value=str(launcher)),patch.object(updater,'run',side_effect=changed),patch.object(updater,'http_json',side_effect=latest),patch('modelhub_kit.cache.process_inventory',return_value=[]):
            self.assertEqual(updater.update({},self.state,selected=['paseo'])[0]['status'],'unmanaged-installation-preserved')
        self.assertFalse((self.state/'tools/paseo.json').exists())

    def test_latest_owned_codex_requires_explicit_repair_of_broken_platform_runtime(self):
        prefix=self.home/'global';package=prefix/'lib/node_modules/@openai/codex';package.mkdir(parents=True)
        metadata=package/'package.json';metadata.write_text(json.dumps({'name':'@openai/codex','version':'1.2.3','bin':{'codex':'codex.js'}}))
        cli=package/'codex.js';cli.write_text('// owned fixture')
        installs=[]
        def command(argv,**options):
            if argv==['npm','prefix','--global']:return {'exit':0,'output':str(prefix)}
            if '-p' in argv:return {'exit':0,'output':json.dumps({'arch':'x64','platform':'linux'})}
            self.fail('Unexpected child command')
        def mutation(argv,**options):
            self.assertEqual(argv[:2],['npm','install'])
            installs.append(argv)
            return {'exit':0,'output':'','treeComplete':True,'observationFailed':False}
        definition={'package':'@openai/codex','command':'codex'}
        with patch.object(updater,'tools',return_value={'codex':definition}),patch.object(updater,'npm_command',return_value=['npm']),patch.object(updater,'owned_installation',return_value=None),patch.object(updater.shutil,'which',side_effect=lambda name:str(cli) if name=='codex' else 'node'),patch.object(updater,'http_json',return_value=({'version':'1.2.3'},{})),patch.object(updater,'run',side_effect=command),patch.object(updater,'run_mutation',side_effect=mutation),patch.object(updater.platform,'system',return_value='Linux'),patch('modelhub_kit.cache.process_inventory',return_value=[]),patch.object(updater,'healthy_codex',side_effect=[False,False,True,True]):
            for _ in range(2):
                self.assertEqual(updater.update({},self.state,selected=['codex'])[0]['status'],'repair-needed')
                self.assertEqual(installs,[])
            self.assertEqual(updater.update({},self.state,selected=['codex'],repair=True)[0]['status'],'updated')
            self.assertEqual(updater.update({},self.state,selected=['codex'])[0]['status'],'current')
        self.assertEqual(len(installs),1)
        self.assertIn('--include=optional',installs[0]);self.assertIn('@openai/codex@1.2.3',installs[0])


class NativeDesktops(unittest.TestCase):
    def test_vendor_apply_uses_mutation_runner_and_waits_for_complete_tree(self):
        app = {'root': '/owned/app', 'version': '1.0.0'}
        after = dict(app, version='2.0.0')
        for complete in (False, True):
            with self.subTest(complete=complete), tempfile.TemporaryDirectory() as folder, \
                    patch.object(desktop, 'discover', side_effect=[app, app, app, after]), \
                    patch.object(desktop, 'process_state', return_value='idle'), \
                    patch.object(desktop, '_linux_plan', return_value={'target': '2.0.0', 'argv': ['vendor', 'install']}), \
                    patch.object(desktop.core, 'run') as query:
                calls = []
                def mutate(argv, **kwargs):
                    calls.append(argv)
                    return {'exit': 0, 'output': '', 'treeComplete': complete, 'timedOut': False, 'observationExceeded': True}
                result = desktop.reconcile('cursor-desktop', home=folder, state=folder,
                                           system='Linux', mutation=mutate)
                self.assertEqual(calls, [['vendor', 'install']])
                self.assertEqual(result['status'], 'updated' if complete else 'queued')
                query.assert_not_called()

    def test_macos_generic_vendor_mutation_queues_without_launch(self):
        app = {'root': '/owned/App.app', 'version': '1.0.0'}
        with tempfile.TemporaryDirectory() as folder, patch.object(desktop, 'discover', return_value=app), \
                patch.object(desktop, 'process_state', return_value='idle'), \
                patch.object(macos_apps, 'cleanup_pending'), \
                patch.object(desktop, '_brew_plan', return_value={'target': '2.0.0', 'argv': ['brew', 'upgrade']}), \
                patch.object(desktop.core, 'run_mutation') as child:
            result = desktop.reconcile('cursor-desktop', home=folder, state=folder, system='Darwin')
            self.assertEqual(result['status'], 'queued')
            self.assertEqual(result['reason'], 'vendor-mutation-custody-unavailable')
            child.assert_not_called()

    def test_winget_source_refresh_is_a_mutation_not_a_query(self):
        calls = []
        def mutation(argv, **kwargs):
            calls.append(argv)
            raise GuardError('fixture-stop-before-any-command')
        with self.assertRaisesRegex(GuardError, 'fixture-stop'):
            desktop._windows_plan('cursor-desktop', None, 'x64', lambda *a, **k: self.fail('query before refresh'),
                                  refresh=True, mutation=mutation)
        self.assertEqual(calls, [['winget', 'source', 'update', '--name', 'winget', '--disable-interactivity']])

    def test_linux_shared_electron_and_denied_visibility_never_report_idle(self):
        proc=MagicMock();proc.iterdir.side_effect=lambda:iter([Path('/proc/123')])
        real_path=Path
        def paths(value):
            return proc if value=='/proc' else real_path(value)
        app={'root':'/usr/share/cursor','version':'1.0.0'}
        with patch.object(desktop.pathlib,'Path',side_effect=paths),patch.object(desktop.os,'readlink') as executable:
            for native in ('/usr/lib/electron42/electron','/usr/bin/electron42','/usr/lib/electron/electron (deleted)'):
                executable.return_value=native
                self.assertEqual(desktop.process_state('cursor-desktop',app,'Linux',None),'unknown')
            executable.return_value='/usr/bin/bash'
            self.assertEqual(desktop.process_state('cursor-desktop',app,'Linux',None),'idle')
            executable.return_value='/usr/share/cursor/cursor'
            self.assertEqual(desktop.process_state('cursor-desktop',app,'Linux',None),'busy')
            executable.side_effect=PermissionError('unreadable live process')
            self.assertEqual(desktop.process_state('cursor-desktop',app,'Linux',None),'unknown')

    def test_macos_pending_cleanup_runs_before_current_return_and_blocks_new_transaction(self):
        app={'root':'/Applications/Cursor.app','version':'2.0.0'}
        with tempfile.TemporaryDirectory() as folder,patch.object(desktop,'discover',return_value=app),patch.object(desktop,'process_state',return_value='idle'),patch.object(desktop,'_brew_plan',return_value={'target':'2.0.0','channel':'homebrew'}),patch.object(macos_apps,'cleanup_pending',return_value=True) as cleaned:
            result=desktop.reconcile('cursor-desktop',home=folder,state=folder,system='Darwin')
            self.assertEqual(result['status'],'current');self.assertEqual(cleaned.call_count,1)
        with tempfile.TemporaryDirectory() as folder,patch.object(macos_apps,'cleanup_pending',side_effect=GuardError('native-dmg-pending-transaction')),patch.object(desktop,'discover') as discovered,patch.object(desktop,'_brew_plan') as planned:
            result=desktop.reconcile('cursor-desktop',home=folder,state=folder,system='Darwin')
            self.assertEqual(result['status'],'queued');discovered.assert_not_called();planned.assert_not_called()

    def test_signed_macos_fallback_applies_only_to_unowned_cask_channel(self):
        old={'root':'/Applications/Cursor.app','version':'1.0.0','metadata':'/Applications/Cursor.app/Contents/Resources/app/product.json'}
        new=dict(old,version='2.0.0')
        plan={'target':'2.0.0','channel':'signed-dmg','_dmg':{'opaque':'internal-only'}}
        with tempfile.TemporaryDirectory() as folder,patch.object(desktop,'discover',side_effect=[old,old,old,new]),patch.object(desktop,'process_state',return_value='idle'),patch.object(desktop,'_brew_plan',side_effect=GuardError('native-homebrew-receipt-does-not-own-bundle')),patch.object(macos_apps,'plan',return_value=plan),patch.object(macos_apps,'apply',return_value={'exit':0,'sha256':'a'*64,'digestSource':'official-https-readback-sha256'}) as applied:
            result=desktop.reconcile('cursor-desktop',home=folder,state=folder,system='Darwin')
            self.assertEqual(result['status'],'updated',result);self.assertEqual(applied.call_count,1)
            receipt=json.loads((Path(folder)/'native-desktop/cursor-desktop.json').read_text())
            self.assertNotIn('_dmg',receipt);self.assertEqual(receipt['sha256'],'a'*64)
        with tempfile.TemporaryDirectory() as folder,patch.object(desktop,'discover',return_value=old),patch.object(desktop,'process_state',return_value='idle'),patch.object(desktop,'_brew_plan',side_effect=GuardError('native-homebrew-install-artifacts-unverified')),patch.object(macos_apps,'plan') as fallback:
            result=desktop.reconcile('cursor-desktop',home=folder,state=folder,system='Darwin')
            self.assertEqual(result['status'],'queued');fallback.assert_not_called()

    def test_arch_static_electron_fuse_never_needs_to_open_grok(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            data=bytearray(binary());data[6]=1
            sentinel=b'dL7pKGdnNz796PbbjQWNKmHXBZaB9tsX'
            for enabled in (False,True):
                path=root/'electron';path.write_bytes(bytes(data)+sentinel+bytes([1,2,0x31 if enabled else 0x30,0x30]))
                self.assertEqual(desktop._elf_fuse(path,'x86_64'),enabled)
            with self.assertRaises(GuardError):desktop._elf_fuse(path,'aarch64')
            package=json.dumps({'name':'sand','productName':'Grok Bot','version':'1.2.3','main':'dist/electron-main/main.cjs'}).encode()
            header=json.dumps({'files':{'package.json':{'size':len(package),'offset':'0'}}}).encode()
            size=8+len(header);(root/'app.asar').write_bytes(struct.pack('<IIII',4,size,size-4,len(header))+header+package)
            self.assertEqual(desktop._asar(root/'app.asar')['version'],'1.2.3')

    def test_desktop_fetch_uses_real_core_tuple_shape(self):
        value={'version':'1.2.3','downloadUrl':'https://downloads.cursor.com/grokbot/stable/linux-x64/app.AppImage'}
        self.assertEqual(desktop.grokbot_release('Linux','x64',lambda url:(value,{'ETag':'opaque'})),'1.2.3')

    def test_arch_deb_data_extraction_rejects_escape_without_running_scripts(self):
        for member in ('usr/share/grok-bot/resources/version.txt','usr/share/grok-bot/../../outside'):
            with self.subTest(member=member),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);artifact=root/'vendor.deb';artifact.write_bytes(b'ar-fixture');vendor=root/'vendor';vendor.mkdir()
                payload=bundle({member:b'1.2.3'});calls=[]
                def command(argv,**options):
                    calls.append(argv)
                    if argv[:2]==['bsdtar','-tf']:return {'exit':0,'output':'debian-binary\ncontrol.tar.gz\ndata.tar.gz\n'}
                    self.assertEqual(argv[:2],['bsdtar','-xf']);(root/'data.tar.gz').write_bytes(payload)
                    return {'exit':0,'output':''}
                if '..' in member:
                    with self.assertRaises(GuardError):desktop.arch_extract(artifact,vendor,'grokbot',command)
                    self.assertFalse((root/'outside').exists())
                else:
                    desktop.arch_extract(artifact,vendor,'grokbot',command)
                    self.assertEqual((vendor/'resources/version.txt').read_text(),'1.2.3')
                self.assertTrue(all(argv[0]=='bsdtar' for argv in calls))

    def test_windows_safe_pinned_inno_manifest_and_missing_digest_refusal(self):
        text = 'Version: 1.2.3\nInstaller Type: inno\nInstaller Url: https://downloads.cursor.com/production/release.exe\nInstaller SHA256: ' + 'a'*64
        self.assertEqual(desktop.parse_winget_show(text,'cursor-desktop')['target'],'1.2.3')
        with self.assertRaises(GuardError): desktop.parse_winget_show(text.replace('inno','exe'),'cursor-desktop')
        with self.assertRaises(GuardError): desktop.parse_winget_show(text.replace('a'*64,''),'cursor-desktop')
        self.assertIn('/NOCLOSEAPPLICATIONS',desktop.SAFE_INNO); self.assertIn('!runcode',desktop.SAFE_INNO)
    def test_busy_desktop_and_missing_app_never_plan_or_start_installer(self):
        app={'root':'/vendor/app','version':'1.0.0','metadata':'/vendor/app/product.json'}
        with tempfile.TemporaryDirectory() as folder, patch.object(desktop,'discover',return_value=app),patch.object(desktop,'process_state',return_value='busy'),patch.object(desktop,'_linux_plan') as plan:
            result=desktop.reconcile('cursor-desktop',home=folder,state=folder,system='Linux')
            self.assertIn('queued',result['status']);plan.assert_not_called()
        with tempfile.TemporaryDirectory() as folder,patch.object(desktop,'discover',return_value=None),patch.object(desktop,'_linux_plan') as plan:
            result=desktop.reconcile('cursor-desktop',home=folder,state=folder,system='Linux')
            self.assertFalse(result['installed']);plan.assert_not_called()
    def test_apt_sources_must_be_exact_signed_vendor_channel(self):
        source='deb [arch=amd64 signed-by=/etc/apt/keyrings/vendor.gpg] https://downloads.cursor.com/aptrepo stable main'
        self.assertTrue(desktop.approved_apt_source(source,'https://downloads.cursor.com/aptrepo'))
        self.assertFalse(desktop.approved_apt_source(source.replace('signed-by=/etc/apt/keyrings/vendor.gpg','trusted=yes'),'https://downloads.cursor.com/aptrepo'))


if __name__ == '__main__':
    unittest.main()
