import unittest
import asyncio
from services.dse_service import DSEMarketService

class TestDSEBoards(unittest.IsolatedAsyncioTestCase):
    async def test_dse_all_boards_integration(self):
        service = DSEMarketService.get_instance()
        
        # 1. Fetch live prices
        res = await service.fetch_live_prices()
        self.assertIn("total_tickers", res)
        self.assertGreaterEqual(res["total_tickers"], 400)
        
        # 2. Boards summary
        boards_data = service.get_boards_summary()
        self.assertIn("boards", boards_data)
        boards_dict = {b["id"]: b for b in boards_data["boards"]}
        
        self.assertIn("PUBLIC", boards_dict)
        self.assertIn("SME", boards_dict)
        self.assertIn("ATB", boards_dict)
        self.assertIn("DEBT", boards_dict)
        self.assertIn("YIELDDBT", boards_dict)
        
        # Public board has > 300 instruments
        self.assertGreaterEqual(boards_dict["PUBLIC"]["count"], 300)
        
        # SME board has 20 instruments
        sme_stocks = service.get_all_stocks(board="SME")
        self.assertGreaterEqual(len(sme_stocks), 15)
        self.assertTrue(any(s["ticker"] == "ACHIASF" for s in sme_stocks))
        
        # ATB board has stocks
        atb_stocks = service.get_all_stocks(board="ATB")
        self.assertGreaterEqual(len(atb_stocks), 2)
        self.assertTrue(any(s["ticker"] == "LBS" for s in atb_stocks))
        
        # DEBT board has stocks
        debt_stocks = service.get_all_stocks(board="DEBT")
        self.assertGreaterEqual(len(debt_stocks), 1)
        
        # YIELDDBT board has government treasury bonds
        gsec_stocks = service.get_all_stocks(board="YIELDDBT")
        self.assertGreaterEqual(len(gsec_stocks), 100)
        
        # 3. Circuit Breakers include Public, SME, and ATB
        cb = await service.get_circuit_breakers()
        self.assertGreater(len(cb), 300)
        boards_in_cb = set(item.get("board") for item in cb)
        self.assertIn("PUBLIC", boards_in_cb)
        self.assertIn("SME", boards_in_cb)
        self.assertIn("ATB", boards_in_cb)

if __name__ == "__main__":
    unittest.main()
