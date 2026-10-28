import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app import main


class AuditHelperTests(unittest.TestCase):
    def test_path_map_uses_longest_prefix(self):
        raw = '[{"remote":"/data","local":"/SSD2TO"},{"remote":"/data/Seedb","local":"/HDD500"}]'
        with patch.dict(os.environ, {"TEST_PATH_MAP": raw}):
            with patch.object(main, "_path_map", wraps=main._path_map):
                with patch.dict(os.environ, {"QBITTORRENT_PATH_MAP": raw}):
                    self.assertEqual(main._map_service_path("QBITTORRENT", "/data/Seedb/movie.mkv"),
                                     "/HDD500/movie.mkv")

    def test_path_map_respects_path_boundaries(self):
        with patch.dict(os.environ, {"RADARR_PATH_MAP": '[{"remote":"/data","local":"/SSD2TO"}]'}):
            self.assertIsNone(main._map_service_path("RADARR", "/database/movie.mkv"))

    def test_hashes_identical_files_with_bounded_helper(self):
        with tempfile.TemporaryDirectory() as temp:
            first = Path(temp) / "first.bin"
            second = Path(temp) / "second.bin"
            first.write_bytes(b"same media bytes")
            second.write_bytes(b"same media bytes")
            cache = {}
            budget = {"bytes": 0, "files": 0}
            self.assertEqual(main._sha256_bounded(first, cache, budget),
                             main._sha256_bounded(second, cache, budget))
            self.assertEqual(budget["files"], 2)

    def test_manual_actions_require_exact_confirmation(self):
        with self.assertRaises(HTTPException):
            main._require_manual_confirmation(main.ManualAction(confirm_text="supprimer"))
        main._require_manual_confirmation(main.ManualAction(confirm_text="SUPPRIMER"))


if __name__ == "__main__":
    unittest.main()
