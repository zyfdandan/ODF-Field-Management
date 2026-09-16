#!/usr/bin/env python3
"""Unit tests for field_images helpers (no live NetBox)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from field_images import media_url_from_attachment, normalize_attachment, parse_device_id


class ParseDeviceIdTests(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(parse_device_id("42"), 42)

    def test_bad(self):
        with self.assertRaises(ValueError):
            parse_device_id("x")
        with self.assertRaises(ValueError):
            parse_device_id("0")


class MediaUrlTests(unittest.TestCase):
    def test_absolute_image_url(self):
        att = {"image": "http://nb/media/image-attachments/a.jpg"}
        self.assertEqual(
            media_url_from_attachment(att, "http://nb"),
            "http://nb/media/image-attachments/a.jpg",
        )

    def test_relative_image_path(self):
        att = {"image": "/media/image-attachments/a.jpg"}
        self.assertEqual(
            media_url_from_attachment(att, "http://nb"),
            "http://nb/media/image-attachments/a.jpg",
        )


class NormalizeTests(unittest.TestCase):
    def test_file_url_is_field_api_path(self):
        att = {"id": 7, "name": "front", "image": "/media/x.jpg"}
        out = normalize_attachment(att)
        self.assertEqual(out["id"], 7)
        self.assertEqual(out["name"], "front")
        self.assertEqual(out["file_url"], "/api/device-images/7/file")


class DeleteTests(unittest.TestCase):
    def test_delete_calls_netbox(self):
        from field_images import delete_device_image

        client = MagicMock()
        client.base_url = "http://nb/api"
        client.session.delete.return_value = MagicMock(status_code=204, text="")
        out = delete_device_image(client, 9)
        self.assertEqual(out, {"ok": True, "id": 9})
        client.session.delete.assert_called_once()
        self.assertIn("/extras/image-attachments/9/", client.session.delete.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
