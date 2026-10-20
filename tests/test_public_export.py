"""Publication selection uses dummy data; no local credentials/history are read."""
from pathlib import Path
import tempfile
import unittest

from scripts.public_export import inspect, candidates, export


class PublicExportTests(unittest.TestCase):
    def test_known_values_are_detected_without_exposing_values(self):
        secret=b'PRIVATE_DUMMY_VALUE'
        self.assertEqual(inspect(b'value="'+secret+b'"',[secret]),['private_local_value'])
        self.assertNotIn(secret.decode(),str(inspect(secret,[secret])))

    def test_short_password_substring_in_digest_is_not_a_disclosure(self):
        self.assertEqual(inspect(b'sha256:aa1234abcdef',[b'1234']),[])
        self.assertEqual(inspect(b'Password=1234',[b'1234']),['private_local_value'])

    def test_shape_and_example_network_checks(self):
        token=b'A'*24+b'.'+b'B'*6+b'.'+b'C'*30
        self.assertIn('token_shape',inspect(token))
        self.assertEqual(inspect(b'loopback=127.0.0.1 example=192.0.2.10'),[])
        # Assemble rather than place a non-example IP literally into public source.
        self.assertIn('non_example_network_address',inspect(b'.'.join([b'10',b'11',b'12',b'13'])))

    def test_export_never_includes_data_history_or_private_deploy_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/'source';root.mkdir()
            good=['README.md','scripts/example.py','deploy/docker/Dockerfile.runtime','deploy/docker/Dockerfile.bot',
                  'deploy/inventory.example.ini','deploy/secrets.example.yml','.dockerignore']
            bad=['config/discord-bot.token','server/ShooterGame/Saved/secret.ini','backups/private.txt','logs/private.txt',
                 '.git/config','deploy/inventory.production.ini','deploy/secrets.yml','deploy/group_vars/private.yml']
            for name in good+bad:
                p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('public_source\n' if name in good else 'private_dummy_value\n')
            selected={p.relative_to(root).as_posix() for p in candidates(root)}
            self.assertEqual(selected,set(good))
            destination=Path(temporary)/'public';result=export(root,destination)
            self.assertFalse(result['git_history_included'])
            self.assertEqual({p.relative_to(destination).as_posix() for p in destination.rglob('*') if p.is_file()},set(good))
            with self.assertRaises(ValueError):export(root,destination)

    def test_symlinked_public_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);(root/'scripts').symlink_to('/tmp')
            with self.assertRaises(ValueError):list(candidates(root))


if __name__=='__main__':unittest.main()
