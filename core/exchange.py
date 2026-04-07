"""
交易所连接模块 — 封装 CCXT，统一对接 OKX（支持现货/永续合约）
"""
import ccxt
import ccxt.async_support as ccxt_async
import pandas as pd
from loguru import logger
from typing import Optional


def _market_type(ex_cfg: dict) -> str:
    return ex_cfg.get("market_type", "spot")


def _build_params(ex_cfg: dict) -> dict:
    """构建 CCXT 初始化参数，兼容 OKX passphrase"""
    params = {
        "apiKey": ex_cfg.get("api_key", ""),
        "secret": ex_cfg.get("api_secret", ""),
        "timeout": ex_cfg.get("timeout", 30000),
        "enableRateLimit": True,
        "options": {"defaultType": _market_type(ex_cfg)},
    }
    passphrase = ex_cfg.get("passphrase", "")
    if passphrase:
        params["password"] = passphrase
    return params


def _build_public_params(ex_cfg: dict) -> dict:
    """只用于拉公开行情，不带 Key，不走 sandbox"""
    return {
        "timeout": ex_cfg.get("timeout", 30000),
        "enableRateLimit": True,
        "options": {"defaultType": _market_type(ex_cfg)},
    }


class Exchange:
    """同步交易所客户端"""

    def __init__(self, config: dict):
        ex_cfg = config["exchange"]
        name = ex_cfg["name"]

        # 私有客户端：用于账户/下单，走 sandbox 设置
        self.client: ccxt.Exchange = getattr(ccxt, name)(_build_params(ex_cfg))
        if ex_cfg.get("sandbox", True):
            self.client.set_sandbox_mode(True)
            logger.info("交易账户已连接模拟盘(Demo Trading)")
        else:
            logger.warning("交易账户已连接正式网络，请注意风险！")

        # 公开行情客户端：始终走正式公开接口，无需 Key
        self.public: ccxt.Exchange = getattr(ccxt, name)(_build_public_params(ex_cfg))
        logger.info("行情数据使用正式公开接口")

        self.markets = None

    def load_markets(self):
        self.markets = self.client.load_markets()
        logger.info(f"已加载 {len(self.markets)} 个交易对")

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1h",
        since: Optional[int] = None,
        limit: int = 1000,
    ) -> pd.DataFrame:
        """拉取 K 线数据（走公开接口）"""
        data = self.public.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
        df = pd.DataFrame(data, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df.set_index("timestamp", inplace=True)
        return df

    def fetch_ohlcv_range(
        self, symbol: str, timeframe: str, start: str, end: str
    ) -> pd.DataFrame:
        """按日期范围分批拉取全量 K 线（走公开接口）"""
        since = int(pd.Timestamp(start).timestamp() * 1000)
        end_ms = int(pd.Timestamp(end).timestamp() * 1000)
        all_data: list[list] = []

        logger.info(f"开始拉取 {symbol} {timeframe} 数据: {start} → {end}")
        while since < end_ms:
            batch = self.public.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
            if not batch:
                break
            all_data.extend(batch)
            since = batch[-1][0] + 1
            logger.debug(f"已拉取 {len(all_data)} 条，最新: {pd.to_datetime(since, unit='ms')}")

        df = pd.DataFrame(all_data, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df.set_index("timestamp", inplace=True)
        df = df[df.index <= pd.Timestamp(end)]
        df = df[~df.index.duplicated(keep="first")]
        logger.info(f"共拉取 {len(df)} 条 K 线")
        return df

    def fetch_ticker(self, symbol: str) -> dict:
        return self.public.fetch_ticker(symbol)

    def set_leverage(self, symbol: str, leverage: int, margin_mode: str = "isolated"):
        """设置合约杠杆和保证金模式"""
        try:
            self.client.set_leverage(leverage, symbol, params={"mgnMode": margin_mode})
            logger.info(f"杠杆设置: {symbol} {leverage}x ({margin_mode})")
        except Exception as e:
            logger.warning("设置杠杆失败（可能已设置）: " + str(e))

    def fetch_balance(self) -> dict:
        return self.client.fetch_balance()

    def create_market_order(self, symbol: str, side: str, amount: float) -> dict:
        logger.info(f"下市价单: {side} {amount} {symbol}")
        return self.client.create_market_order(symbol, side, amount)

    def create_limit_order(self, symbol: str, side: str, amount: float, price: float) -> dict:
        logger.info(f"下限价单: {side} {amount} {symbol} @ {price}")
        return self.client.create_limit_order(symbol, side, amount, price)

    def cancel_order(self, order_id: str, symbol: str) -> dict:
        return self.client.cancel_order(order_id, symbol)

    def fetch_open_orders(self, symbol: Optional[str] = None) -> list:
        return self.client.fetch_open_orders(symbol)

    def fetch_order(self, order_id: str, symbol: str) -> dict:
        return self.client.fetch_order(order_id, symbol)


class AsyncExchange:
    """异步交易所客户端（用于实盘轮询）"""

    def __init__(self, config: dict):
        ex_cfg = config["exchange"]
        name = ex_cfg["name"]

        # 私有客户端：账户/下单，走 sandbox
        self.client: ccxt_async.Exchange = getattr(ccxt_async, name)(_build_params(ex_cfg))
        self._sandbox = ex_cfg.get("sandbox", True)

        # 公开行情客户端：始终走正式公开接口
        self.public: ccxt_async.Exchange = getattr(ccxt_async, name)(_build_public_params(ex_cfg))

    async def init(self):
        if self._sandbox:
            self.client.set_sandbox_mode(True)
        await self.client.load_markets()
        await self.public.load_markets()

    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 200) -> pd.DataFrame:
        data = await self.public.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(data, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df.set_index("timestamp", inplace=True)
        return df

    async def fetch_ticker(self, symbol: str) -> dict:
        return await self.public.fetch_ticker(symbol)

    async def set_leverage(self, symbol: str, leverage: int, margin_mode: str = "isolated"):
        try:
            await self.client.set_leverage(leverage, symbol, params={"mgnMode": margin_mode})
            logger.info(f"杠杆设置: {symbol} {leverage}x ({margin_mode})")
        except Exception as e:
            logger.warning("设置杠杆失败（可能已设置）: " + str(e))

    async def fetch_balance(self) -> dict:
        return await self.client.fetch_balance()

    async def create_market_order(self, symbol: str, side: str, amount: float) -> dict:
        logger.info(f"[LIVE] 下市价单: {side} {amount} {symbol}")
        return await self.client.create_market_order(symbol, side, amount)

    async def create_limit_order(self, symbol: str, side: str, amount: float, price: float) -> dict:
        logger.info(f"[LIVE] 下限价单: {side} {amount} {symbol} @ {price}")
        return await self.client.create_limit_order(symbol, side, amount, price)

    async def cancel_order(self, order_id: str, symbol: str) -> dict:
        return await self.client.cancel_order(order_id, symbol)

    async def close(self):
        await self.client.close()
        await self.public.close()
