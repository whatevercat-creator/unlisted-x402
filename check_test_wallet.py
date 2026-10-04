"""
Read-only check of the outbound test wallet on Base mainnet: its USDC
balance and its current USDC allowance to Permit2. Sends no transaction
and signs nothing.

    python check_test_wallet.py 0xWALLET_ADDRESS
    python check_test_wallet.py            # looks the address up in CDP

Without an address it looks up the wallet named by
X402_DOCTOR_OUTBOUND_TEST_WALLET (or outbound_payment's default name) with
CDP's get_account -- a lookup that never creates an account -- using the
CDP credentials in the environment. Only addresses and amounts are printed.

Addresses, ABIs and the RPC URL come from the SDKs and outbound_payment.py.
"""

from __future__ import annotations

import asyncio
import os
import sys

from cdp.network_config import NETWORK_TO_RPC_URL
from web3 import Web3
from x402.mechanisms.evm.constants import BALANCE_OF_ABI, ERC20_ALLOWANCE_ABI, PERMIT2_ADDRESS

from outbound_payment import DEFAULT_TEST_WALLET_NAME, USDC_BASE_ADDRESS, _USDC_DECIMALS


async def _lookup_address(name: str) -> str:
    from cdp import CdpClient

    async with CdpClient() as cdp:
        return (await cdp.evm.get_account(name=name)).address


def main() -> None:
    if len(sys.argv) > 1:
        wallet = sys.argv[1]
    else:
        name = os.environ.get("X402_DOCTOR_OUTBOUND_TEST_WALLET") or DEFAULT_TEST_WALLET_NAME
        wallet = asyncio.run(_lookup_address(name))

    w3 = Web3(Web3.HTTPProvider(NETWORK_TO_RPC_URL["base"]))
    usdc = w3.eth.contract(
        address=Web3.to_checksum_address(USDC_BASE_ADDRESS), abi=BALANCE_OF_ABI + ERC20_ALLOWANCE_ABI
    )
    owner = Web3.to_checksum_address(wallet)
    balance = usdc.functions.balanceOf(owner).call()
    allowance = usdc.functions.allowance(owner, Web3.to_checksum_address(PERMIT2_ADDRESS)).call()

    print(f"wallet:             {owner}")
    print(f"USDC balance:       {balance / 10**_USDC_DECIMALS:.6f} USDC")
    print(f"Permit2 allowance:  {allowance} (raw units; 0 means no live approval)")
    print("approval history:   enter the wallet at https://basescan.org/tokenapprovalchecker")


if __name__ == "__main__":
    main()
