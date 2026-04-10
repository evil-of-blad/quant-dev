"""
套利仓位修复工具
检查现货和合约的实际数量是否对齐，不对齐则提示如何修复

用法:
    venv/bin/python scripts/rebalance_arb.py            # 只检查
    venv/bin/python scripts/rebalance_arb.py --fix      # 检查 + 自动平衡
"""
import sys
import os
import asyncio
import argparse
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ccxt.async_support as ccxt


async def main(do_fix: bool):
    with open("config/config.yaml") as f:
        config = yaml.safe_load(f)

    ex_cfg = config["exchange"]
    ex = ccxt.okx({
        "apiKey": ex_cfg["api_key"],
        "secret": ex_cfg["api_secret"],
        "password": ex_cfg["passphrase"],
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
    })
    if ex_cfg.get("sandbox"):
        ex.set_sandbox_mode(True)

    await ex.load_markets()

    arb_cfg = config.get("funding_arb", {})
    symbols = arb_cfg.get("symbols", [])

    print("=" * 70)
    print("套利仓位检查")
    print("=" * 70)

    balance = await ex.fetch_balance()
    positions = await ex.fetch_positions(symbols)

    issues = []

    for symbol in symbols:
        base_coin = symbol.split("/")[0]
        market = ex.markets.get(symbol, {})
        contract_size = float(market.get("contractSize", 1) or 1)

        # 现货持仓
        spot_amount = balance.get(base_coin, {}).get("total", 0) or 0

        # 合约空单（张数）
        swap_contracts = 0.0
        for p in positions:
            if p.get("symbol") == symbol and p.get("side") == "short":
                swap_contracts = abs(float(p.get("contracts", 0) or 0))
                break

        # 合约对应的 base 数量
        swap_base = swap_contracts * contract_size

        print(f"\n【{symbol}】 (1张 = {contract_size} {base_coin})")
        print(f"  现货持仓: {spot_amount:.6f} {base_coin}")
        print(f"  合约空单: {swap_contracts} 张 = {swap_base:.6f} {base_coin}")
        print(f"  差值:     {abs(spot_amount - swap_base):.6f} {base_coin}")

        # 判断是否平衡（5%容差）
        if spot_amount < 1e-9 and swap_contracts < 1e-9:
            print(f"  状态: ⚪ 无仓位")
            continue

        max_side = max(spot_amount, swap_base)
        diff = abs(spot_amount - swap_base)
        if diff < max_side * 0.05:
            print(f"  状态: ✅ 对冲平衡")
            continue

        print(f"  状态: ❌ 失衡!")

        # 修复建议
        if spot_amount > swap_base:
            extra_spot = spot_amount - swap_base
            print(f"  → 现货过多 {extra_spot:.6f}")
            print(f"    方案A: 卖出现货 {extra_spot:.6f} {base_coin}")

            # 补空合约方案
            need_contracts_total = spot_amount / contract_size
            # 按精度
            precision = market.get("precision", {}).get("amount", 1)
            if isinstance(precision, int):
                factor = 10 ** precision
                need_contracts_total = int(need_contracts_total * factor) / factor
            else:
                need_contracts_total = int(need_contracts_total / precision) * precision

            add_contracts = need_contracts_total - swap_contracts
            print(f"    方案B: 加空合约 {add_contracts:.4f} 张")

            issues.append({
                "symbol": symbol,
                "base_coin": base_coin,
                "type": "spot_excess",
                "extra_spot": extra_spot,
                "add_contracts": add_contracts,
            })
        else:
            extra_swap = swap_base - spot_amount
            print(f"  → 合约空单过多 {extra_swap:.6f}")
            print(f"    方案A: 平空合约 {extra_swap / contract_size:.4f} 张")
            print(f"    方案B: 加买现货 {extra_swap:.6f} {base_coin}")

            issues.append({
                "symbol": symbol,
                "base_coin": base_coin,
                "type": "swap_excess",
                "extra_swap": extra_swap,
                "close_contracts": extra_swap / contract_size,
            })

    print(f"\n{'=' * 70}")
    if not issues:
        print("✅ 所有仓位都对冲平衡")
    else:
        print(f"❌ 发现 {len(issues)} 个失衡仓位")

    if issues and do_fix:
        print(f"\n开始自动修复（方案A：调整现货数量匹配合约）...")
        for issue in issues:
            symbol = issue["symbol"]
            base_coin = issue["base_coin"]
            spot_symbol = symbol.replace(":USDT", "")

            try:
                if issue["type"] == "spot_excess":
                    extra = issue["extra_spot"]
                    print(f"  卖出 {spot_symbol} {extra:.6f}...")
                    await ex.create_market_order(
                        spot_symbol, "sell", extra,
                        params={"tdMode": "cash"}
                    )
                    print(f"  ✅ {symbol} 已平衡")
                else:
                    close = issue["close_contracts"]
                    print(f"  平空 {symbol} {close:.4f} 张...")
                    await ex.create_market_order(
                        symbol, "buy", close,
                        params={"tdMode": "isolated"}
                    )
                    print(f"  ✅ {symbol} 已平衡")
            except Exception as e:
                print(f"  ❌ 修复失败: {e}")
    elif issues:
        print(f"\n如需自动修复（采用方案A），运行:")
        print(f"  venv/bin/python scripts/rebalance_arb.py --fix")

    await ex.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fix", action="store_true", help="执行自动修复")
    args = parser.parse_args()
    asyncio.run(main(args.fix))
