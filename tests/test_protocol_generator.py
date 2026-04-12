import importlib.util
import pathlib
import unittest

from gen_racelink_proto_py import generate


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_module_from_path(module_name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class ProtocolGeneratorTests(unittest.TestCase):
    def test_generator_writes_package_module_contract(self):
        out_path = ROOT / "tests" / "_generated_racelink_proto_auto_test.py"
        try:
            generate(ROOT / "racelink_proto.h", out_path)
            generated = _load_module_from_path("generated_racelink_proto_auto_test", out_path)
        finally:
            if out_path.exists():
                out_path.unlink()

        self.assertEqual(generated.PROTO_VER_MAJOR, 1)
        self.assertEqual(generated.PROTO_VER_MINOR, 4)
        self.assertEqual(generated.SZ_P_IdentifyReply, 9)
        self.assertEqual(generated.SZ_P_StatusReply, 8)
        self.assertEqual(generated.SZ_P_Ack, 3)
        self.assertEqual(
            generated.STRUCT_FIELDS["P_IdentifyReply"],
            [("fw", "uint8_t", 1), ("caps", "uint8_t", 1), ("groupId", "uint8_t", 1), ("mac6", "uint8_t", 6)],
        )
        self.assertEqual(
            generated.STRUCT_FIELDS["P_Stream"],
            [("ctrl", "uint8_t", 1), ("data", "uint8_t", 8)],
        )

        rules_by_name = {rule.name: rule for rule in generated.RULES}
        self.assertEqual(rules_by_name["DEVICES/IDENTIFY"].req_len, generated.SZ_P_GetDevices)
        self.assertEqual(rules_by_name["DEVICES/IDENTIFY"].rsp_len, generated.SZ_P_IdentifyReply)
        self.assertEqual(rules_by_name["STATUS"].rsp_len, generated.SZ_P_StatusReply)

    def test_generator_default_output_path_points_into_package(self):
        source = (ROOT / "gen_racelink_proto_py.py").read_text(encoding="utf-8")
        self.assertIn('default="racelink/racelink_proto_auto.py"', source)


if __name__ == "__main__":
    unittest.main()
