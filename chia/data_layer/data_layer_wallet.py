from __future__ import annotations

import logging
import json
import time
import dataclasses
from operator import attrgetter
from typing import Any, Optional, Tuple, Set, List, Dict, Type, TypeVar, TYPE_CHECKING

from blspy import G2Element
from clvm.EvalError import EvalError

from chia.consensus.block_record import BlockRecord
from chia.protocols.wallet_protocol import PuzzleSolutionResponse, CoinState
from chia.wallet.db_wallet.db_wallet_puzzles import (
    ACS_MU,
    ACS_MU_PH,
    create_host_fullpuz,
    SINGLETON_LAUNCHER,
    create_host_layer_puzzle,
    launch_solution_to_singleton_info,
    launcher_to_struct,
    match_dl_singleton,
    create_graftroot_offer_puz,
    GRAFTROOT_DL_OFFERS,
)
from chia.types.announcement import Announcement
from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program, SerializedProgram
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.coin_spend import CoinSpend
from chia.types.condition_opcodes import ConditionOpcode
from chia.types.spend_bundle import SpendBundle
from chia.util.ints import uint8, uint32, uint64, uint128
from chia.util.json_util import dict_to_json_str
from chia.util.streamable import Streamable, streamable
from chia.wallet.derivation_record import DerivationRecord
from chia.wallet.outer_puzzles import AssetType
from chia.wallet.puzzle_drivers import PuzzleInfo, Solver
from chia.wallet.puzzles.singleton_top_layer_v1_1 import SINGLETON_LAUNCHER_HASH
from chia.wallet.sign_coin_spends import sign_coin_spends
from chia.wallet.trading.offer import Offer, NotarizedPayment
from chia.wallet.transaction_record import TransactionRecord
from chia.wallet.wallet_coin_record import WalletCoinRecord
from chia.wallet.lineage_proof import LineageProof
from chia.wallet.util.compute_memos import compute_memos
from chia.wallet.util.merkle_utils import simplify_merkle_proof
from chia.wallet.util.transaction_type import TransactionType
from chia.wallet.util.wallet_types import AmountWithPuzzlehash, WalletType
from chia.wallet.wallet import Wallet
from chia.wallet.wallet_info import WalletInfo

if TYPE_CHECKING:
    from chia.wallet.wallet_state_manager import WalletStateManager


@streamable
@dataclasses.dataclass(frozen=True)
class SingletonRecord(Streamable):
    coin_id: bytes32
    launcher_id: bytes32
    root: bytes32
    inner_puzzle_hash: bytes32
    confirmed: bool
    confirmed_at_height: uint32
    lineage_proof: LineageProof
    generation: uint32
    timestamp: uint64


_T_DataLayerWallet = TypeVar("_T_DataLayerWallet", bound="DataLayerWallet")


class DataLayerWallet:
    wallet_state_manager: WalletStateManager
    log: logging.Logger
    wallet_info: WalletInfo
    wallet_id: uint8
    standard_wallet: Wallet
    """
    interface used by datalayer for interacting with the chain
    """

    @classmethod
    async def create(
        cls: Type[_T_DataLayerWallet],
        wallet_state_manager: WalletStateManager,
        wallet: Wallet,
        wallet_info: WalletInfo,
        name: Optional[str] = None,
    ) -> _T_DataLayerWallet:
        self = cls()
        self.wallet_state_manager = wallet_state_manager
        self.log = logging.getLogger(name if name else __name__)
        self.standard_wallet = wallet
        self.wallet_info = wallet_info
        self.wallet_id = uint8(self.wallet_info.id)

        return self

    @classmethod
    def type(cls) -> uint8:
        return uint8(WalletType.DATA_LAYER)

    def id(self) -> uint32:
        return self.wallet_info.id

    @classmethod
    async def create_new_dl_wallet(
        cls: Type[_T_DataLayerWallet],
        wallet_state_manager: WalletStateManager,
        wallet: Wallet,
        name: Optional[str] = "DataLayer Wallet",
        in_transaction: bool = False,
    ) -> _T_DataLayerWallet:
        """
        This must be called under the wallet state manager lock
        """

        self = cls()
        self.wallet_state_manager = wallet_state_manager
        self.log = logging.getLogger(name if name else __name__)
        self.standard_wallet = wallet

        for _, wallet in self.wallet_state_manager.wallets.items():
            if wallet.type() == uint8(WalletType.DATA_LAYER):
                raise ValueError("DataLayer Wallet already exists for this key")

        assert name is not None
        maybe_wallet_info = await wallet_state_manager.user_store.create_wallet(
            name,
            WalletType.DATA_LAYER.value,
            "",
            in_transaction=in_transaction,
        )
        if maybe_wallet_info is None:
            raise ValueError("Internal Error")
        self.wallet_info = maybe_wallet_info
        self.wallet_id = uint8(self.wallet_info.id)

        await self.wallet_state_manager.add_new_wallet(self, self.wallet_info.id, in_transaction=in_transaction)

        return self

    #############
    # LAUNCHING #
    #############

    @staticmethod
    async def match_dl_launcher(launcher_spend: CoinSpend) -> Tuple[bool, Optional[bytes32]]:
        # Sanity check it's a launcher
        if launcher_spend.puzzle_reveal.to_program() != SINGLETON_LAUNCHER:
            return False, None

        # Let's make sure the solution looks how we expect it to
        try:
            full_puzhash, amount, root, inner_puzhash = launch_solution_to_singleton_info(
                launcher_spend.solution.to_program()
            )
        except ValueError:
            return False, None

        # Now let's check that the full puzzle is an odd data layer singleton
        if (
            full_puzhash
            != create_host_fullpuz(inner_puzhash, root, launcher_spend.coin.name()).get_tree_hash(inner_puzhash)
            or amount % 2 == 0
        ):
            return False, None

        return True, inner_puzhash

    async def get_launcher_coin_state(self, launcher_id: bytes32) -> CoinState:
        coin_states: List[CoinState] = await self.wallet_state_manager.wallet_node.get_coin_state([launcher_id])

        if len(coin_states) == 0:
            raise ValueError(f"Launcher ID {launcher_id} is not a valid coin")
        if coin_states[0].coin.puzzle_hash != SINGLETON_LAUNCHER.get_tree_hash():
            raise ValueError(f"Coin with ID {launcher_id} is not a singleton launcher")
        if coin_states[0].created_height is None:
            raise ValueError(f"Launcher with ID {launcher_id} has not been created (maybe reorged)")
        if coin_states[0].spent_height is None:
            raise ValueError(f"Launcher with ID {launcher_id} has not been spent")

        return coin_states[0]

    async def track_new_launcher_id(  # This is the entry point for non-owned singletons
        self,
        launcher_id: bytes32,
        spend: Optional[CoinSpend] = None,
        height: Optional[uint32] = None,
        in_transaction: bool = False,
    ) -> None:
        if await self.wallet_state_manager.dl_store.get_launcher(launcher_id) is not None:
            self.log.info(f"Spend of launcher {launcher_id} has already been processed")
            return None
        if spend is not None and spend.coin.name() == launcher_id:  # spend.coin.name() == launcher_id is a sanity check
            await self.new_launcher_spend(spend, height, in_transaction)
        else:
            launcher_state: CoinState = await self.get_launcher_coin_state(launcher_id)

            data: Dict[str, Any] = {
                "data": {
                    "action_data": {
                        "api_name": "request_puzzle_solution",
                        "height": launcher_state.spent_height,
                        "coin_name": launcher_id,
                        "launcher_coin": {
                            "parent_id": launcher_state.coin.parent_coin_info.hex(),
                            "puzzle_hash": launcher_state.coin.puzzle_hash.hex(),
                            "amount": str(launcher_state.coin.amount),
                        },
                    }
                }
            }

            data_str = dict_to_json_str(data)
            await self.wallet_state_manager.create_action(
                name="request_puzzle_solution",
                wallet_id=self.id(),
                wallet_type=self.type(),
                callback="new_launcher_spend_response",
                done=False,
                data=data_str,
                in_transaction=False,  # We should never be fetching this during sync, it will provide us with the spend
            )

    async def new_launcher_spend_response(self, response: PuzzleSolutionResponse, action_id: int) -> None:
        action = await self.wallet_state_manager.action_store.get_wallet_action(action_id)
        assert action is not None
        coin_dict = json.loads(action.data)["data"]["action_data"]["launcher_coin"]
        launcher_coin = Coin(
            bytes32.from_hexstr(coin_dict["parent_id"]),
            bytes32.from_hexstr(coin_dict["puzzle_hash"]),
            uint64(int(coin_dict["amount"])),
        )
        await self.new_launcher_spend(
            CoinSpend(launcher_coin, response.puzzle, response.solution),
            height=response.height,
        )

    async def new_launcher_spend(
        self,
        launcher_spend: CoinSpend,
        height: Optional[uint32] = None,
        in_transaction: bool = False,
    ) -> None:
        launcher_id: bytes32 = launcher_spend.coin.name()
        if height is None:
            height = (await self.get_launcher_coin_state(launcher_id)).spent_height
            assert height is not None
        full_puzhash, amount, root, inner_puzhash = launch_solution_to_singleton_info(
            launcher_spend.solution.to_program()
        )
        new_singleton = Coin(launcher_id, full_puzhash, amount)

        singleton_record: Optional[SingletonRecord] = await self.wallet_state_manager.dl_store.get_latest_singleton(
            launcher_id
        )
        if singleton_record is not None:
            if (  # This is an unconfirmed singleton that we know about
                singleton_record.coin_id == new_singleton.name() and not singleton_record.confirmed
            ):
                timestamp = await self.wallet_state_manager.wallet_node.get_timestamp_for_height(height)
                await self.wallet_state_manager.dl_store.set_confirmed(singleton_record.coin_id, height, timestamp)
            else:
                self.log.info(f"Spend of launcher {launcher_id} has already been processed")
                return None
        else:
            timestamp = await self.wallet_state_manager.wallet_node.get_timestamp_for_height(height)
            await self.wallet_state_manager.dl_store.add_singleton_record(
                SingletonRecord(
                    coin_id=new_singleton.name(),
                    launcher_id=launcher_id,
                    root=root,
                    inner_puzzle_hash=inner_puzhash,
                    confirmed=True,
                    confirmed_at_height=height,
                    timestamp=timestamp,
                    lineage_proof=LineageProof(
                        launcher_id,
                        create_host_layer_puzzle(inner_puzhash, root).get_tree_hash(inner_puzhash),
                        amount,
                    ),
                    generation=uint32(0),
                ),
                in_transaction,
            )

        await self.wallet_state_manager.dl_store.add_launcher(launcher_spend.coin, in_transaction)
        await self.wallet_state_manager.add_interested_puzzle_hashes([launcher_id], [self.id()], in_transaction)
        await self.wallet_state_manager.add_interested_coin_ids([new_singleton.name()], in_transaction)
        await self.wallet_state_manager.coin_store.add_coin_record(
            WalletCoinRecord(
                new_singleton,
                height,
                uint32(0),
                False,
                False,
                WalletType(self.type()),
                self.id(),
            )
        )

    ################
    # TRANSACTIONS #
    ################

    async def generate_new_reporter(
        self,
        initial_root: bytes32,
        fee: uint64 = uint64(0),
    ) -> Tuple[TransactionRecord, TransactionRecord, bytes32]:
        """
        Creates the initial singleton, which includes spending an origin coin, the launcher, and creating a singleton
        """

        coins: Set[Coin] = await self.standard_wallet.select_coins(uint64(fee + 1))
        if coins is None:
            raise ValueError("Not enough coins to create new data layer singleton")

        launcher_parent: Coin = list(coins)[0]
        launcher_coin: Coin = Coin(launcher_parent.name(), SINGLETON_LAUNCHER.get_tree_hash(), uint64(1))

        inner_puzzle: Program = await self.standard_wallet.get_new_puzzle()
        full_puzzle: Program = create_host_fullpuz(inner_puzzle, initial_root, launcher_coin.name())

        genesis_launcher_solution: Program = Program.to(
            [full_puzzle.get_tree_hash(), 1, [initial_root, inner_puzzle.get_tree_hash()]]
        )
        announcement_message: bytes32 = genesis_launcher_solution.get_tree_hash()
        announcement = Announcement(launcher_coin.name(), announcement_message)
        create_launcher_tx_record: Optional[TransactionRecord] = await self.standard_wallet.generate_signed_transaction(
            amount=uint64(1),
            puzzle_hash=SINGLETON_LAUNCHER.get_tree_hash(),
            fee=fee,
            origin_id=launcher_parent.name(),
            coins=coins,
            primaries=None,
            ignore_max_send_amount=False,
            coin_announcements_to_consume={announcement},
        )
        assert create_launcher_tx_record is not None and create_launcher_tx_record.spend_bundle is not None

        launcher_cs: CoinSpend = CoinSpend(
            launcher_coin,
            SerializedProgram.from_program(SINGLETON_LAUNCHER),
            SerializedProgram.from_program(genesis_launcher_solution),
        )
        launcher_sb: SpendBundle = SpendBundle([launcher_cs], G2Element())
        full_spend: SpendBundle = SpendBundle.aggregate([create_launcher_tx_record.spend_bundle, launcher_sb])

        # Delete from standard transaction so we don't push duplicate spends
        std_record: TransactionRecord = dataclasses.replace(create_launcher_tx_record, spend_bundle=None)
        dl_record = TransactionRecord(
            confirmed_at_height=uint32(0),
            created_at_time=uint64(int(time.time())),
            to_puzzle_hash=bytes32([2] * 32),
            amount=uint64(1),
            fee_amount=fee,
            confirmed=False,
            sent=uint32(10),
            spend_bundle=full_spend,
            additions=full_spend.additions(),
            removals=full_spend.removals(),
            memos=list(compute_memos(full_spend).items()),
            wallet_id=uint32(0),  # This is being called before the wallet is created so we're using a temp ID of 0
            sent_to=[],
            trade_id=None,
            type=uint32(TransactionType.INCOMING_TX.value),
            name=full_spend.name(),
        )
        singleton_record = SingletonRecord(
            coin_id=Coin(launcher_coin.name(), full_puzzle.get_tree_hash(), uint64(1)).name(),
            launcher_id=launcher_coin.name(),
            root=initial_root,
            inner_puzzle_hash=inner_puzzle.get_tree_hash(),
            confirmed=False,
            confirmed_at_height=uint32(0),
            timestamp=uint64(0),
            lineage_proof=LineageProof(
                launcher_coin.name(),
                create_host_layer_puzzle(inner_puzzle, initial_root).get_tree_hash(),
                uint64(1),
            ),
            generation=uint32(0),
        )

        await self.wallet_state_manager.dl_store.add_singleton_record(singleton_record, False)
        await self.wallet_state_manager.add_interested_puzzle_hashes([singleton_record.launcher_id], [self.id()], False)

        return dl_record, std_record, launcher_coin.name()

    async def create_tandem_xch_tx(
        self,
        fee: uint64,
        announcement_to_assert: Announcement,
        coin_announcement: bool = True,
        in_transaction: bool = False,
    ) -> TransactionRecord:
        chia_tx = await self.standard_wallet.generate_signed_transaction(
            amount=uint64(0),
            puzzle_hash=await self.standard_wallet.get_new_puzzlehash(in_transaction=in_transaction),
            fee=fee,
            negative_change_allowed=False,
            coin_announcements_to_consume={announcement_to_assert} if coin_announcement else None,
            puzzle_announcements_to_consume=None if coin_announcement else {announcement_to_assert},
            in_transaction=in_transaction,
        )
        assert chia_tx.spend_bundle is not None
        return chia_tx

    async def create_update_state_spend(
        self,
        launcher_id: bytes32,
        root_hash: Optional[bytes32],
        new_puz_hash: Optional[bytes32] = None,
        new_amount: Optional[uint64] = None,
        fee: uint64 = uint64(0),
        coin_announcements_to_consume: Optional[Set[Announcement]] = None,
        puzzle_announcements_to_consume: Optional[Set[Announcement]] = None,
        sign: bool = True,
        add_pending_singleton: bool = True,
        announce_new_state: bool = False,
        in_transaction: bool = False,
    ) -> List[TransactionRecord]:
        singleton_record, parent_lineage = await self.get_spendable_singleton_info(launcher_id)

        if root_hash is None:
            root_hash = singleton_record.root

        inner_puzzle_derivation: Optional[
            DerivationRecord
        ] = await self.wallet_state_manager.puzzle_store.get_derivation_record_for_puzzle_hash(
            singleton_record.inner_puzzle_hash
        )
        if inner_puzzle_derivation is None:
            raise ValueError(f"DL Wallet does not have permission to update Singleton with launcher ID {launcher_id}")

        # Make the child's puzzles
        if new_puz_hash is None:
            new_puz_hash = (await self.standard_wallet.get_new_puzzle(in_transaction=in_transaction)).get_tree_hash()
        assert new_puz_hash is not None
        next_full_puz_hash: bytes32 = create_host_fullpuz(new_puz_hash, root_hash, launcher_id).get_tree_hash(
            new_puz_hash
        )

        # Construct the current puzzles
        current_inner_puzzle: Program = self.standard_wallet.puzzle_for_pk(inner_puzzle_derivation.pubkey)
        current_full_puz = create_host_fullpuz(
            current_inner_puzzle,
            singleton_record.root,
            launcher_id,
        )
        current_coin = Coin(
            singleton_record.lineage_proof.parent_name,
            current_full_puz.get_tree_hash(),
            singleton_record.lineage_proof.amount,
        )
        assert singleton_record.lineage_proof.parent_name is not None
        assert singleton_record.lineage_proof.amount is not None

        new_singleton_record = SingletonRecord(
            coin_id=Coin(current_coin.name(), next_full_puz_hash, singleton_record.lineage_proof.amount).name(),
            launcher_id=launcher_id,
            root=root_hash,
            inner_puzzle_hash=new_puz_hash,
            confirmed=False,
            confirmed_at_height=uint32(0),
            timestamp=uint64(0),
            lineage_proof=LineageProof(
                singleton_record.coin_id,
                new_puz_hash,
                singleton_record.lineage_proof.amount,
            ),
            generation=uint32(singleton_record.generation + 1),
        )

        # Optionally add an ephemeral spend to announce
        if announce_new_state:
            announce_only: Program = Program.to(
                (
                    1,
                    [
                        [
                            51,
                            new_puz_hash,
                            singleton_record.lineage_proof.amount,
                            [launcher_id, root_hash, new_puz_hash],
                        ],
                        [62, b"$"],
                    ],
                )
            )
            second_full_puz: Program = create_host_fullpuz(
                announce_only,
                root_hash,
                launcher_id,
            )
            second_coin = Coin(
                current_coin.name(), second_full_puz.get_tree_hash(), singleton_record.lineage_proof.amount
            )
            second_coin_spend = CoinSpend(
                second_coin,
                second_full_puz.to_serialized_program(),
                Program.to(
                    [
                        LineageProof(
                            current_coin.parent_coin_info,
                            create_host_layer_puzzle(current_inner_puzzle, singleton_record.root).get_tree_hash(),
                            singleton_record.lineage_proof.amount,
                        ).to_program(),
                        singleton_record.lineage_proof.amount,
                        [[]],
                    ]
                ),
            )
            root_announce = Announcement(second_full_puz.get_tree_hash(), b"$")
            if puzzle_announcements_to_consume is None:
                puzzle_announcements_to_consume = [root_announce]
            else:
                puzzle_announcements_to_consume.append(root_announce)
            second_singleton_record = SingletonRecord(
                coin_id=second_coin.name(),
                launcher_id=launcher_id,
                root=root_hash,
                inner_puzzle_hash=announce_only.get_tree_hash(),
                confirmed=False,
                confirmed_at_height=uint32(0),
                timestamp=uint64(0),
                lineage_proof=LineageProof(
                    second_coin.parent_coin_info,
                    announce_only.get_tree_hash(),
                    singleton_record.lineage_proof.amount,
                ),
                generation=uint32(singleton_record.generation + 1),
            )
            new_singleton_record = dataclasses.replace(
                new_singleton_record,
                coin_id=Coin(second_coin.name(), next_full_puz_hash, singleton_record.lineage_proof.amount).name(),
                lineage_proof=LineageProof(
                    second_coin.name(),
                    next_full_puz_hash,
                    singleton_record.lineage_proof.amount,
                ),
                generation=uint32(second_singleton_record.generation + 1),
            )

        # Create the solution
        primaries: List[AmountWithPuzzlehash] = [
            {
                "puzzlehash": announce_only.get_tree_hash() if announce_new_state else new_puz_hash,
                "amount": singleton_record.lineage_proof.amount if new_amount is None else new_amount,
                "memos": [launcher_id, root_hash, new_puz_hash],
            }
        ]
        inner_sol: Program = self.standard_wallet.make_solution(
            primaries=primaries,
            coin_announcements={b"$"} if fee > 0 else None,
            coin_announcements_to_assert={a.name() for a in coin_announcements_to_consume}
            if coin_announcements_to_consume is not None
            else None,
            puzzle_announcements_to_assert={a.name() for a in puzzle_announcements_to_consume}
            if puzzle_announcements_to_consume is not None
            else None,
        )
        if root_hash != singleton_record.root:
            magic_condition = Program.to([-24, ACS_MU, [[Program.to((root_hash, None)), ACS_MU_PH], None]])
            # TODO: This line is a hack, make_solution should allow us to pass extra conditions to it
            inner_sol = Program.to([[], (1, magic_condition.cons(inner_sol.at("rfr"))), []])
        db_layer_sol = Program.to([inner_sol])
        full_sol = Program.to(
            [
                parent_lineage.to_program(),
                singleton_record.lineage_proof.amount,
                db_layer_sol,
            ]
        )

        # Create the spend
        coin_spend = CoinSpend(
            current_coin,
            SerializedProgram.from_program(current_full_puz),
            SerializedProgram.from_program(full_sol),
        )
        await self.standard_wallet.hack_populate_secret_key_for_puzzle_hash(current_inner_puzzle.get_tree_hash())

        if sign:
            spend_bundle = await self.sign(coin_spend)
        else:
            spend_bundle = SpendBundle([coin_spend], G2Element())

        if announce_new_state:
            spend_bundle = dataclasses.replace(spend_bundle, coin_spends=[coin_spend, second_coin_spend])

        dl_tx = TransactionRecord(
            confirmed_at_height=uint32(0),
            created_at_time=uint64(int(time.time())),
            to_puzzle_hash=new_puz_hash,
            amount=uint64(singleton_record.lineage_proof.amount),
            fee_amount=fee,
            confirmed=False,
            sent=uint32(10),
            spend_bundle=spend_bundle,
            additions=spend_bundle.additions(),
            removals=spend_bundle.removals(),
            memos=list(compute_memos(spend_bundle).items()),
            wallet_id=self.id(),
            sent_to=[],
            trade_id=None,
            type=uint32(TransactionType.OUTGOING_TX.value),
            name=singleton_record.coin_id,
        )
        if fee > 0:
            chia_tx = await self.create_tandem_xch_tx(
                fee, Announcement(current_coin.name(), b"$"), coin_announcement=True, in_transaction=in_transaction
            )
            aggregate_bundle = SpendBundle.aggregate([dl_tx.spend_bundle, chia_tx.spend_bundle])
            dl_tx = dataclasses.replace(dl_tx, spend_bundle=aggregate_bundle)
            chia_tx = dataclasses.replace(chia_tx, spend_bundle=None)
            txs: List[TransactionRecord] = [dl_tx, chia_tx]
        else:
            txs = [dl_tx]

        if add_pending_singleton:
            await self.wallet_state_manager.dl_store.add_singleton_record(
                new_singleton_record,
                in_transaction=in_transaction,
            )
            if announce_new_state:
                await self.wallet_state_manager.dl_store.add_singleton_record(
                    second_singleton_record,
                    in_transaction=in_transaction,
                )

        return txs

    async def generate_signed_transaction(
        self,
        amounts: List[uint64],
        puzzle_hashes: List[bytes32],
        fee: uint64 = uint64(0),
        coins: Set[Coin] = set(),
        memos: Optional[List[List[bytes]]] = None,  # ignored
        coin_announcements_to_consume: Optional[Set[Announcement]] = None,
        puzzle_announcements_to_consume: Optional[Set[Announcement]] = None,
        ignore_max_send_amount: bool = False,  # ignored
        # This wallet only
        launcher_id: Optional[bytes32] = None,
        new_root_hash: Optional[bytes32] = None,
        sign: bool = True,  # This only prevent signing of THIS wallet's part of the tx (fee will still be signed)
        add_pending_singleton: bool = True,
        announce_new_state: bool = False,
    ) -> List[TransactionRecord]:
        # Figure out the launcher ID
        if len(coins) == 0:
            if launcher_id is None:
                raise ValueError("Not enough info to know which DL coin to send")
        else:
            if len(coins) != 1:
                raise ValueError("The wallet can only send one DL coin at a time")
            else:
                record = await self.wallet_state_manager.dl_store.get_singleton_record(next(iter(coins)).name())
                if record is None:
                    raise ValueError("The specified coin is not a tracked DL")
                else:
                    launcher_id = record.launcher_id

        if len(amounts) != 1 or len(puzzle_hashes) != 1:
            raise ValueError("The wallet can only send one DL coin to one place at a time")

        return await self.create_update_state_spend(
            launcher_id,
            new_root_hash,
            puzzle_hashes[0],
            amounts[0],
            fee,
            coin_announcements_to_consume,
            puzzle_announcements_to_consume,
            sign,
            add_pending_singleton,
            announce_new_state,
        )

    async def get_spendable_singleton_info(self, launcher_id: bytes32) -> Tuple[SingletonRecord, LineageProof]:
        # First, let's make sure this is a singleton that we track and that we can spend
        singleton_record: Optional[SingletonRecord] = await self.get_latest_singleton(launcher_id)
        if singleton_record is None:
            raise ValueError(f"Singleton with launcher ID {launcher_id} is not tracked by DL Wallet")

        # Next, the singleton should be confirmed or else we shouldn't be ready to spend it
        if not singleton_record.confirmed:
            raise ValueError(f"Singleton with launcher ID {launcher_id} is currently pending")

        # Next, let's verify we have all of the relevant coin information
        if singleton_record.lineage_proof.parent_name is None or singleton_record.lineage_proof.amount is None:
            raise ValueError(f"Singleton with launcher ID {launcher_id} has insufficient information to spend")

        # Finally, let's get the parent record for its lineage proof
        parent_singleton: Optional[SingletonRecord] = await self.wallet_state_manager.dl_store.get_singleton_record(
            singleton_record.lineage_proof.parent_name
        )
        parent_lineage: LineageProof
        if parent_singleton is None:
            if singleton_record.lineage_proof.parent_name != launcher_id:
                raise ValueError(f"Have not found the parent of singleton with launcher ID {launcher_id}")
            else:
                launcher_coin: Optional[Coin] = await self.wallet_state_manager.dl_store.get_launcher(launcher_id)
                if launcher_coin is None:
                    raise ValueError(f"DL Wallet does not have launcher info for id {launcher_id}")
                else:
                    parent_lineage = LineageProof(launcher_coin.parent_coin_info, None, uint64(launcher_coin.amount))
        else:
            parent_lineage = parent_singleton.lineage_proof

        return singleton_record, parent_lineage

    async def get_owned_singletons(self) -> List[SingletonRecord]:
        launcher_ids = await self.wallet_state_manager.dl_store.get_all_launchers()

        collected = []

        for launcher_id in launcher_ids:
            singleton_record = await self.wallet_state_manager.dl_store.get_latest_singleton(launcher_id=launcher_id)
            if singleton_record is None:
                # this is likely due to a race between getting the list and acquiring the extra data
                continue

            inner_puzzle_derivation: Optional[
                DerivationRecord
            ] = await self.wallet_state_manager.puzzle_store.get_derivation_record_for_puzzle_hash(
                singleton_record.inner_puzzle_hash
            )
            if inner_puzzle_derivation is not None:
                collected.append(singleton_record)

        return collected

    ###########
    # SYNCING #
    ###########

    async def singleton_removed(self, parent_spend: CoinSpend, height: uint32, in_transaction: bool = False) -> None:
        parent_name = parent_spend.coin.name()
        puzzle = parent_spend.puzzle_reveal
        solution = parent_spend.solution

        matched, _ = match_dl_singleton(puzzle.to_program())
        if matched:
            self.log.info(f"DL singleton removed: {parent_spend.coin}")
            singleton_record: Optional[SingletonRecord] = await self.wallet_state_manager.dl_store.get_singleton_record(
                parent_name
            )
            if singleton_record is None:
                self.log.warning(f"DL wallet received coin it does not have parent for. Expected parent {parent_name}.")
                return

            # Information we need to create the singleton record
            full_puzzle_hash: bytes32
            amount: uint64
            root: bytes32
            inner_puzzle_hash: bytes32

            conditions = puzzle.run_with_cost(self.wallet_state_manager.constants.MAX_BLOCK_COST_CLVM, solution)[
                1
            ].as_python()
            found_singleton: bool = False
            for condition in conditions:
                if condition[0] == ConditionOpcode.CREATE_COIN and int.from_bytes(condition[2], "big") % 2 == 1:
                    full_puzzle_hash = bytes32(condition[1])
                    amount = uint64(int.from_bytes(condition[2], "big"))
                    try:
                        root = bytes32(condition[3][1])
                        inner_puzzle_hash = bytes32(condition[3][2])
                    except IndexError:
                        self.log.warning(
                            f"Parent {parent_name} with launcher {singleton_record.launcher_id} "
                            "did not hint its child properly"
                        )
                        return
                    found_singleton = True
                    break

            if not found_singleton:
                self.log.warning(f"Singleton with launcher ID {singleton_record.launcher_id} was melted")
                return

            new_singleton = Coin(parent_name, full_puzzle_hash, amount)
            timestamp = await self.wallet_state_manager.wallet_node.get_timestamp_for_height(height)
            await self.wallet_state_manager.dl_store.add_singleton_record(
                SingletonRecord(
                    coin_id=new_singleton.name(),
                    launcher_id=singleton_record.launcher_id,
                    root=root,
                    inner_puzzle_hash=inner_puzzle_hash,
                    confirmed=True,
                    confirmed_at_height=height,
                    timestamp=timestamp,
                    lineage_proof=LineageProof(
                        parent_name,
                        create_host_layer_puzzle(inner_puzzle_hash, root).get_tree_hash(inner_puzzle_hash),
                        amount,
                    ),
                    generation=uint32(singleton_record.generation + 1),
                ),
                True,
            )
            await self.wallet_state_manager.coin_store.add_coin_record(
                WalletCoinRecord(
                    new_singleton,
                    height,
                    uint32(0),
                    False,
                    False,
                    WalletType(self.type()),
                    self.id(),
                )
            )
            await self.wallet_state_manager.add_interested_coin_ids(
                [new_singleton.name()],
                in_transaction=in_transaction,
            )
            await self.potentially_handle_resubmit(singleton_record.launcher_id, in_transaction=in_transaction)

    async def potentially_handle_resubmit(self, launcher_id: bytes32, in_transaction: bool = False) -> None:
        """
        This method is meant to detect a fork in our expected pending singletons and the singletons that have actually
        been confirmed on chain.  If there is a fork and the root on chain never changed, we will attempt to rebase our
        singletons on to the new latest singleton.  If there is a fork and the root changed, we assume that everything
        has failed and delete any pending state.
        """
        unconfirmed_singletons = await self.wallet_state_manager.dl_store.get_unconfirmed_singletons(launcher_id)
        if len(unconfirmed_singletons) == 0:
            return
        unconfirmed_singletons = sorted(unconfirmed_singletons, key=attrgetter("generation"))
        full_branch: List[SingletonRecord] = await self.wallet_state_manager.dl_store.get_all_singletons_for_launcher(
            launcher_id,
            min_generation=unconfirmed_singletons[0].generation,
        )
        if len(unconfirmed_singletons) == len(full_branch) and set(unconfirmed_singletons) == set(full_branch):
            return

        # Now we have detected a fork so we should check whether the root changed at all
        self.log.info("Attempting automatic rebase")
        parent_name = unconfirmed_singletons[0].lineage_proof.parent_name
        assert parent_name is not None
        parent_singleton = await self.wallet_state_manager.dl_store.get_singleton_record(parent_name)
        if parent_singleton is None or any(parent_singleton.root != s.root for s in full_branch if s.confirmed):
            root_changed: bool = True
        else:
            root_changed = False

        # Regardless of whether the root changed or not, our old state is bad so let's eliminate it
        # First let's find all of our txs matching our unconfirmed singletons
        relevant_dl_txs: List[TransactionRecord] = []
        for singleton in unconfirmed_singletons:
            parent_name = singleton.lineage_proof.parent_name
            if parent_name is None:
                continue

            tx = await self.wallet_state_manager.tx_store.get_transaction_record(parent_name)
            if tx is not None:
                relevant_dl_txs.append(tx)
        # Let's check our standard wallet for fee transactions related to these dl txs
        all_spends: List[SpendBundle] = [tx.spend_bundle for tx in relevant_dl_txs if tx.spend_bundle is not None]
        all_removal_ids: Set[bytes32] = {removal.name() for sb in all_spends for removal in sb.removals()}
        unconfirmed_std_txs: List[
            TransactionRecord
        ] = await self.wallet_state_manager.tx_store.get_unconfirmed_for_wallet(self.standard_wallet.id())
        relevant_std_txs: List[TransactionRecord] = [
            tx for tx in unconfirmed_std_txs if any(c.name() in all_removal_ids for c in tx.removals)
        ]
        # Delete all of the relevant transactions
        for tx in [*relevant_dl_txs, *relevant_std_txs]:
            await self.wallet_state_manager.tx_store.delete_transaction_record(tx.name)
        # Delete all of the unconfirmed singleton records
        for singleton in unconfirmed_singletons:
            await self.wallet_state_manager.dl_store.delete_singleton_record(singleton.coin_id)

        if not root_changed:
            # The root never changed so let's attempt a rebase
            try:
                all_txs: List[TransactionRecord] = []
                for singleton in unconfirmed_singletons:
                    for tx in relevant_dl_txs:
                        if any(c.name() == singleton.coin_id for c in tx.additions):
                            if tx.spend_bundle is not None:
                                fee = uint64(tx.spend_bundle.fees())
                            else:
                                fee = uint64(0)

                            all_txs.extend(
                                await self.create_update_state_spend(
                                    launcher_id,
                                    singleton.root,
                                    fee=fee,
                                    in_transaction=in_transaction,
                                )
                            )
                for tx in all_txs:
                    await self.wallet_state_manager.add_pending_transaction(tx, in_transaction=in_transaction)
            except Exception as e:
                self.log.warning(f"Something went wrong during attempted DL resubmit: {str(e)}")
                # Something went wrong so let's delete anything pending that was created
                for singleton in unconfirmed_singletons:
                    await self.wallet_state_manager.dl_store.delete_singleton_record(singleton.coin_id)

    async def stop_tracking_singleton(self, launcher_id: bytes32) -> None:
        await self.wallet_state_manager.dl_store.delete_singleton_records_by_launcher_id(launcher_id)
        await self.wallet_state_manager.dl_store.delete_launcher(launcher_id)

    ###########
    # UTILITY #
    ###########

    async def get_latest_singleton(
        self, launcher_id: bytes32, only_confirmed: bool = False
    ) -> Optional[SingletonRecord]:
        singleton: Optional[SingletonRecord] = await self.wallet_state_manager.dl_store.get_latest_singleton(
            launcher_id, only_confirmed
        )
        return singleton

    async def get_history(
        self,
        launcher_id: bytes32,
        min_generation: Optional[uint32] = None,
        max_generation: Optional[uint32] = None,
        num_results: Optional[uint32] = None,
    ) -> List[SingletonRecord]:
        history: List[SingletonRecord] = await self.wallet_state_manager.dl_store.get_all_singletons_for_launcher(
            launcher_id,
            min_generation,
            max_generation,
            num_results,
        )
        return history

    async def get_singleton_record(self, coin_id: bytes32) -> Optional[SingletonRecord]:
        singleton: Optional[SingletonRecord] = await self.wallet_state_manager.dl_store.get_singleton_record(coin_id)
        return singleton

    async def get_singletons_by_root(self, launcher_id: bytes32, root: bytes32) -> List[SingletonRecord]:
        singletons: List[SingletonRecord] = await self.wallet_state_manager.dl_store.get_singletons_by_root(
            launcher_id, root
        )
        return singletons

    ##########
    # WALLET #
    ##########

    def puzzle_for_pk(self, pubkey: bytes) -> Program:
        return self.standard_wallet.puzzle_for_pk(pubkey)

    async def get_new_puzzle(self) -> Program:
        return self.puzzle_for_pk(
            bytes((await self.wallet_state_manager.get_unused_derivation_record(self.wallet_info.id)).pubkey)
        )

    async def new_peak(self, peak: BlockRecord) -> None:
        pass

    async def get_confirmed_balance(self, record_list: Optional[Set[WalletCoinRecord]] = None) -> uint64:
        return uint64(0)

    async def get_unconfirmed_balance(self, record_list: Optional[Set[WalletCoinRecord]] = None) -> uint128:
        return uint128(0)

    async def get_spendable_balance(self, unspent_records: Optional[Set[WalletCoinRecord]] = None) -> uint128:
        return uint128(0)

    async def get_pending_change_balance(self) -> uint64:
        return uint64(0)

    async def get_max_send_amount(self, unspent_records: Optional[Set[WalletCoinRecord]] = None) -> uint128:
        return uint128(0)

    async def sign(self, coin_spend: CoinSpend) -> SpendBundle:
        return await sign_coin_spends(
            [coin_spend],
            self.standard_wallet.secret_key_store.secret_key_for_public_key,
            self.wallet_state_manager.constants.AGG_SIG_ME_ADDITIONAL_DATA,
            self.wallet_state_manager.constants.MAX_BLOCK_COST_CLVM,
        )

    ##########
    # OFFERS #
    ##########

    async def get_puzzle_info(self, launcher_id: bytes32) -> PuzzleInfo:
        record = await self.get_latest_singleton(launcher_id)
        if record is None:
            raise ValueError(f"DL wallet does not know about launcher ID {launcher_id}")
        return PuzzleInfo(
            {
                "type": AssetType.SINGLETON.value,
                "launcher_id": "0x" + launcher_id.hex(),
                "launcher_ph": "0x" + SINGLETON_LAUNCHER_HASH.hex(),
                "also": {
                    "type": AssetType.METADATA.value,
                    "metadata": f"(0x{record.root} . ())",
                    "updater_hash": "0x" + ACS_MU_PH.hex(),
                },
            }
        )

    async def get_coins_to_offer(self, launcher_id: bytes32, amount: uint64) -> Set[Coin]:
        record = await self.get_latest_singleton(launcher_id)
        if record is None:
            raise ValueError(f"DL wallet does not know about launcher ID {launcher_id}")
        puzhash: bytes32 = create_host_fullpuz(record.inner_puzzle_hash, record.root, launcher_id).get_tree_hash(
            record.inner_puzzle_hash
        )
        assert record.lineage_proof.parent_name is not None
        assert record.lineage_proof.amount is not None
        return set([Coin(record.lineage_proof.parent_name, puzhash, record.lineage_proof.amount)])

    @staticmethod
    async def make_update_offer(
        wallet_state_manager: Any,
        offer_dict: Dict[Optional[bytes32], int],
        driver_dict: Dict[bytes32, PuzzleInfo],
        solver: Solver,
        fee: uint64 = uint64(0),
    ) -> Offer:
        dl_wallet = None
        for wallet in wallet_state_manager.wallets.values():
            if wallet.type() == WalletType.DATA_LAYER.value:
                dl_wallet = wallet
                break
        if dl_wallet is None:
            raise ValueError("DL Wallet is not initialized")

        offered_launchers: List[bytes32] = [k for k, v in offer_dict.items() if v < 0 and k is not None]
        fee_left_to_pay: uint64 = fee
        all_bundles: List[SpendBundle] = []
        for launcher in offered_launchers:
            try:
                this_solver: Solver = solver[launcher.hex()]
            except KeyError:
                this_solver = solver["0x" + launcher.hex()]
            new_root: bytes32 = this_solver["new_root"]
            new_ph: bytes32 = await wallet_state_manager.main_wallet.get_new_puzzlehash()
            txs: List[TransactionRecord] = await dl_wallet.generate_signed_transaction(
                [uint64(1)],
                [new_ph],
                fee=fee_left_to_pay,
                launcher_id=launcher,
                new_root_hash=new_root,
                sign=False,
                add_pending_singleton=False,
                announce_new_state=True,
            )
            fee_left_to_pay = uint64(0)

            assert txs[0].spend_bundle is not None
            dl_spend: CoinSpend = next(
                cs for cs in txs[0].spend_bundle.coin_spends if match_dl_singleton(cs.puzzle_reveal.to_program())[0]
            )
            all_other_spends: List[CoinSpend] = [cs for cs in txs[0].spend_bundle.coin_spends if cs != dl_spend]
            dl_solution: Program = dl_spend.solution.to_program()
            old_graftroot: Program = dl_solution.at("rrffrf")
            new_graftroot: Program = create_graftroot_offer_puz(
                [bytes32.from_hexstr(k) for k in this_solver["dependencies"].info.keys()],
                [list(bytes32.from_hexstr(v) for v in values) for values in this_solver["dependencies"].info.values()],
                old_graftroot,
            )

            new_solution: Program = dl_solution.replace(rrffrf=new_graftroot, rrffrrf=Program.to([None] * 5))
            new_spend: CoinSpend = dataclasses.replace(
                dl_spend,
                solution=new_solution.to_serialized_program(),
            )
            signed_bundle = await dl_wallet.sign(new_spend)
            new_bundle: SpendBundle = dataclasses.replace(
                txs[0].spend_bundle,
                coin_spends=all_other_spends,
            )
            all_bundles.append(SpendBundle.aggregate([signed_bundle, new_bundle]))

        # create some dummy requested payments
        requested_payments = {
            k: [NotarizedPayment(bytes32([0] * 32), uint64(v), [], bytes32([0] * 32))]
            for k, v in offer_dict.items()
            if v > 0
        }
        return Offer(requested_payments, SpendBundle.aggregate(all_bundles), driver_dict)

    @staticmethod
    async def finish_graftroot_solutions(offer: Offer, solver: Solver) -> Offer:
        # Build a mapping of launcher IDs to their new innerpuz
        singleton_to_innerpuzhash: Dict[bytes32, bytes32] = {}
        innerpuzhash_to_root = {}
        all_parent_ids: List[bytes32] = [cs.coin.parent_coin_info for cs in offer.bundle.coin_spends]
        for spend in offer.bundle.coin_spends:
            matched, curried_args = match_dl_singleton(spend.puzzle_reveal.to_program())
            if matched and spend.coin.name() not in all_parent_ids:
                innerpuz, temp_root, launcher_id = curried_args
                innerpuzhash_to_root[innerpuz.get_tree_hash()] = temp_root.as_python()
                singleton_to_innerpuzhash[
                    launcher_to_struct(bytes32(launcher_id.as_python())).get_tree_hash()
                ] = innerpuz.get_tree_hash()

        # Create all of the new solutions
        new_spends: List[CoinSpend] = []
        for spend in offer.bundle.coin_spends:
            solution = spend.solution.to_program()
            if match_dl_singleton(spend.puzzle_reveal.to_program())[0]:
                try:
                    graftroot: Program = solution.at("rrffrf")
                except EvalError:
                    new_spends.append(spend)
                    continue
                mod, curried_args = graftroot.uncurry()
                if mod == GRAFTROOT_DL_OFFERS:
                    _, singleton_structs, _, values_to_prove = curried_args.as_iter()
                    all_proofs = []
                    roots = []
                    for values in values_to_prove.as_python():
                        asserted_root: Optional[str] = None
                        proofs_of_inclusion = []
                        for value in values:
                            for root in solver["proofs_of_inclusion"].info:
                                proof: Tuple[int, List[bytes32]] = tuple(  # type: ignore
                                    solver["proofs_of_inclusion"][root]
                                )
                                if simplify_merkle_proof(value, proof) == bytes32.from_hexstr(root):
                                    proofs_of_inclusion.append(proof)
                                    if asserted_root is None:
                                        asserted_root = root
                                    elif asserted_root != root:
                                        raise ValueError("Malformed DL offer")
                                    break
                        roots.append(asserted_root)
                        all_proofs.append(proofs_of_inclusion)
                    new_solution: Program = solution.replace(
                        rrffrrf=Program.to(
                            [
                                all_proofs,
                                [Program.to((bytes32.from_hexstr(root), None)) for root in roots if root is not None],
                                [ACS_MU_PH] * len(all_proofs),
                                [
                                    singleton_to_innerpuzhash[struct.get_tree_hash()]
                                    for struct in singleton_structs.as_iter()
                                ],
                                solution.at("rrffrrfrrrrf"),
                            ]
                        )
                    )
                    new_spend: CoinSpend = dataclasses.replace(
                        spend,
                        solution=new_solution.to_serialized_program(),
                    )
                    spend = new_spend
            new_spends.append(spend)

        return Offer({}, SpendBundle(new_spends, offer.bundle.aggregated_signature), offer.driver_dict)

    @staticmethod
    async def get_offer_summary(offer: Offer) -> Dict[str, Any]:
        summary: Dict[str, Any] = {"offered": []}
        for spend in offer.bundle.coin_spends:
            solution = spend.solution.to_program()
            matched, curried_args = match_dl_singleton(spend.puzzle_reveal.to_program())
            if matched:
                try:
                    graftroot: Program = solution.at("rrffrf")
                except EvalError:
                    continue
                mod, graftroot_curried_args = graftroot.uncurry()
                if mod == GRAFTROOT_DL_OFFERS:
                    child_spend: CoinSpend = next(
                        cs for cs in offer.bundle.coin_spends if cs.coin.parent_coin_info == spend.coin.name()
                    )
                    _, child_curried_args = match_dl_singleton(child_spend.puzzle_reveal.to_program())
                    singleton_summary = {
                        "launcher_id": list(curried_args)[2].as_python().hex(),
                        "new_root": list(child_curried_args)[1].as_python().hex(),
                        "dependencies": [],
                    }
                    _, singleton_structs, _, values_to_prove = graftroot_curried_args.as_iter()
                    for struct, values in zip(singleton_structs.as_iter(), values_to_prove.as_iter()):
                        singleton_summary["dependencies"].append(
                            {
                                "launcher_id": struct.at("rf").as_python().hex(),
                                "values_to_prove": [value.as_python().hex() for value in values.as_iter()],
                            }
                        )
                    summary["offered"].append(singleton_summary)
        return summary
