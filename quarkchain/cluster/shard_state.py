from quarkchain.cluster.core import RootBlock, MinorBlock, CrossShardTransactionList, CrossShardTransactionDeposit
from quarkchain.cluster.genesis import create_genesis_minor_block, create_genesis_root_block
from quarkchain.config import NetworkId
from quarkchain.core import calculate_merkle_root, Address, Constant
from quarkchain.evm.state import State as EvmState
from quarkchain.evm.messages import apply_transaction
from quarkchain.evm import opcodes
from quarkchain.reward import ConstMinorBlockRewardCalcultor
from quarkchain.utils import Logger, check


class ShardDb:
    def __init__(self, db):
        self.db = db
        # TODO:  iterate db to recover pools and set
        self.mHeaderPool = dict()
        self.mMetaPool = dict()
        self.xShardSet = set()
        self.rHeaderPool = dict()

    # ------------------------- Root block db operations --------------------------------
    def putRootBlock(self, rootBlock, rootBlockHash=None):
        if rootBlockHash is None:
            rootBlockHash = rootBlock.header.getHash()

        self.db.put(b"rblock_" + rootBlockHash, rootBlock.serialize())
        self.rHeaderPool[rootBlockHash] = rootBlock.header

    def getRootBlockByHash(self, h):
        return RootBlock.deserialize(self.db.get(b"rblock_" + h))

    def getRootBlockHeaderByHash(self, h):
        return self.rHeaderPool.get(h)

    def containRootBlockByHash(self, h):
        return h in self.rHeaderPool

    # ------------------------- Minor block db operations --------------------------------
    def putMinorBlock(self, mBlock, evmState, mBlockHash=None):
        if mBlockHash is None:
            mBlockHash = mBlock.header.getHash()

        self.db.put(b"mblock_" + mBlockHash, mBlock.serialize())
        self.db.put(b"state_" + mBlockHash, evmState.trie.root_hash)
        self.mHeaderPool[mBlockHash] = mBlock.header
        self.mMetaPool[mBlockHash] = mBlock.meta

    def getMinorBlockHeaderByHash(self, h):
        return self.mHeaderPool.get(h)

    def getMinorBlockEvmRootHashByHash(self, h):
        return self.db.get(b"state_" + h)

    def getMinorBlockMetaByHash(self, h):
        return self.mMetaPool.get(h)

    def getMinorBlockByHash(self, h):
        return MinorBlock.deserialize(self.db.get(b"mblock_" + h))

    def containMinorBlockByHash(self, h):
        return h in self.mHeaderPool

    # -------------------------- Cross-shard tx operations ----------------------------
    def putMinorBlockXshardTxList(self, h, txList):
        self.xShardSet.add(h)
        self.db.put(b"xShard_" + h, txList.serialize())

    def getMinorBlockXshardTxList(self, h):
        return CrossShardTransactionList.deserialize(self.db.get(b"xShard_" + h))

    def containRemoteMinorBlockHash(self, h):
        return h in self.xShardSet

    def put(self, key, value):
        self.db.put(key, value)

    def get(self, key, default=None):
        return self.db.get(key, default)

    def __getitem__(self, key):
        return self[key]


class ShardState:
    """  State of a shard, which includes
    - evm state
    - minor blockchain
    - root blockchain and cross-shard transaction
    TODO: Support
    - reshard by split
    """

    def __init__(self, env, shardId, createGenesis=False, db=None):
        self.env = env
        self.diffCalc = self.env.config.MINOR_DIFF_CALCULATOR
        self.diffHashFunc = self.env.config.DIFF_HASH_FUNC
        self.rewardCalc = ConstMinorBlockRewardCalcultor(env)
        self.rawDb = db if db is not None else env.db
        self.db = ShardDb(self.rawDb)

        check(createGenesis)
        if createGenesis:
            self.__createGenesisBlocks(shardId)

        # TODO: Query db to recover the latest state

    def __createGenesisBlocks(self, shardId):
        genesisRootBlock = create_genesis_root_block(self.env)
        genesisMinorBlock = create_genesis_minor_block(
            env=self.env,
            shardId=shardId,
            hashRootBlock=genesisRootBlock.header.getHash())

        self.evmState = EvmState(env=self.env.evmEnv, db=self.db)
        self.evmState.block_coinbase = genesisMinorBlock.meta.coinbaseAddress.recipient
        self.evmState.delta_balance(
            self.evmState.block_coinbase,
            self.env.config.GENESIS_MINOR_COIN)
        self.evmState.commit()

        self.branch = genesisMinorBlock.header.branch
        self.db.putMinorBlock(genesisMinorBlock, self.evmState)
        self.db.putRootBlock(genesisRootBlock)

        self.rootTip = genesisRootBlock.header
        # Tips that are confirmed by root
        self.confirmedHeaderTip = genesisMinorBlock.header
        self.confirmedMetaTip = genesisMinorBlock.header
        # Tips that are unconfirmed by root
        self.headerTip = genesisMinorBlock.header
        self.metaTip = genesisMinorBlock.meta

    def __performTx(self, tx, evmState):
        # UTXOs are not supported now
        if len(tx.inList) != 0:
            raise RuntimeError("input list must be empty")
        if len(tx.outList) != 0:
            raise RuntimeError("output list must be empty")
        if len(tx.signList) != 0:
            raise RuntimeError("sign list must be empty")

        # Check OP code
        if len(tx.code.code) == 0:
            raise RuntimeError("empty op code")
        if not tx.code.isEvm():
            raise RuntimeError("only evm transaction is supported now")

        evmTx = tx.code.getEvmTransaction()
        if self.branch.value != evmTx.branchValue:
            raise RuntimeError("evm tx is not in the shard")
        if evmTx.getWithdraw() < 0:
            raise RuntimeError("withdraw must be non-negative")
        if evmTx.getWithdraw() != 0:
            if len(evmTx.withdrawTo) != Constant.ADDRESS_LENGTH:
                raise ValueError("withdraw to address length is incorrect")
            withdrawTo = Address.deserialize(evmTx.withdrawTo)
            if self.branch.isInShard(withdrawTo.fullShardId):
                raise ValueError("withdraw address must not in the shard")
        # TODO: Neighborhood and xshard gas limit check

        success, output = apply_transaction(evmState, evmTx)
        return success, output

    def __getEvmStateForNewBlock(self, block):
        state = EvmState(env=self.env.evmEnv)
        state.trie.root_hash = self.db.getMinorBlockEvmRootHashByHash(block.header.hashPrevMinorBlock)
        state.txindex = 0
        state.gas_used = 0
        state.bloom = 0
        state.receipts = []
        state.timestamp = block.header.createTime
        state.gas_limit = block.meta.evmGasLimit  # TODO
        state.block_number = block.header.height
        state.recent_uncles[state.block_number] = []  # TODO [x.hash for x in block.uncles]
        # TODO: Create a account with shard info if the account is not created
        state.block_coinbase = block.meta.coinbaseAddress.recipient
        state.block_difficulty = block.header.difficulty
        state.block_reward = 0
        state.prev_headers = []                          # TODO: state.add_block_header(block.header)
        return state

    def prevalidateBlock(self, block):
        ''' Validate a block before running evm transactions
        '''
        if not self.db.containMinorBlockByHash(block.header.hashPrevMinorBlock):
            # TODO:  May put the block back to queue
            raise ValueError("prev block not found")
        prevHeader = self.db.getMinorBlockHeaderByHash(block.header.hashPrevMinorBlock)
        prevMeta = self.db.getMinorBlockMetaByHash(block.header.hashPrevMinorBlock)

        if block.header.height != prevHeader.height + 1:
            raise ValueError("height mismatch")

        if block.header.branch != self.branch:
            raise ValueError("branch mismatch")

        if block.header.createTime <= prevHeader.createTime:
            raise ValueError("incorrect create time tip time {}, new block time {}".format(
                block.header.createTime, self.chain[-1].createTime))

        if block.header.hashMeta != block.meta.getHash():
            raise ValueError("Hash of meta mismatch")

        if len(block.meta.extraData) > self.env.config.BLOCK_EXTRA_DATA_SIZE_LIMIT:
            raise ValueError("extraData in block is too large")

        # Make sure merkle tree is valid
        merkleHash = calculate_merkle_root(block.txList)
        if merkleHash != block.meta.hashMerkleRoot:
            raise ValueError("incorrect merkle root")

        # Check the first transaction of the block
        if not self.branch.isInShard(block.meta.coinbaseAddress.fullShardId):
            raise ValueError("coinbase output address must be in the shard")

        # Check difficulty
        if not self.env.config.SKIP_MINOR_DIFFICULTY_CHECK:
            if self.env.config.NETWORK_ID == NetworkId.MAINNET:
                diff = self.getNextBlockDifficulty(block.header.createTime)
                metric = diff * int.from_bytes(block.header.getHash(), byteorder="big")
                if metric >= 2 ** 256:
                    raise ValueError("incorrect difficulty")
            elif block.meta.coinbaseAddress.recipient != self.env.config.TESTNET_MASTER_ACCOUNT.recipient:
                raise ValueError("incorrect master to create the block")

        if not self.branch.isInShard(block.meta.coinbaseAddress.fullShardId):
            raise ValueError("coinbase output must be in local shard")

        # Check whether the root header is in the root chain
        rootBlockHeader = self.db.getRootBlockHeaderByHash(block.meta.hashPrevRootBlock)
        if rootBlockHeader is None:
            raise ValueError("cannot find root block for the minor block")

        if rootBlockHeader.height < self.db.getRootBlockHeaderByHash(prevMeta.hashPrevRootBlock).height:
            raise ValueError("prev root block height must be non-decreasing")

    def runBlock(self, block, evmState=None):
        if evmState is None:
            evmState = self.__getEvmStateForNewBlock(block)
        rootBlockHeader = self.db.getRootBlockHeaderByHash(block.meta.hashPrevRootBlock)
        prevMeta = self.db.getMinorBlockMetaByHash(block.header.hashPrevMinorBlock)

        self.__runCrossShardTxList(
            evmState=evmState,
            descendantRootHeader=rootBlockHeader,
            ancestorRootHeader=self.db.getRootBlockHeaderByHash(prevMeta.hashPrevRootBlock))

        for idx, tx in enumerate(block.txList):
            try:
                self.__performTx(tx, evmState)
            except Exception as e:
                Logger.debugException()
                Logger.debug("failed to process Tx {}, idx {}, reason {}".format(
                    tx.getHash().hex(), idx, e))
                raise e

        # Put only half of block fee to coinbase address
        check(evmState.get_balance(evmState.block_coinbase) >= evmState.block_fee)
        evmState.delta_balance(evmState.block_coinbase, -evmState.block_fee // 2)

        # Update actual root hash
        evmState.commit()
        return evmState

    def __isMinorBlockLinkedToRootTip(self, mBlock):
        """ Determine whether a minor block is a descendant of a minor block confirmed by root tip
        """
        if mBlock.header.height <= self.confirmedHeaderTip.height:
            return False

        header = mBlock.header
        for i in range(self.confirmedHeaderTip.height - mBlock.header.height):
            header = self.db.getMinorBlockHeaderByHash(header.hashPrevMinorBlock)

        return header == self.confirmedHeaderTip

    def addBlock(self, block):
        """  Add a block to local db.  Perform validate and update tip accordingly
        """

        if self.db.containMinorBlockByHash(block.header.getHash()):
            return None

        # Throw exception if fail to run
        self.prevalidateBlock(block)
        evmState = self.runBlock(block)

        # ------------------------ Validate ending result of the block --------------------
        if block.meta.hashEvmStateRoot != evmState.trie.root_hash:
            raise ValueError("State root mismatch: header %s computed %s" %
                             (block.meta.hashEvmStateRoot.hex(), evmState.trie.root_hash.hex()))

        if evmState.gas_used != block.meta.evmGasUsed:
            raise ValueError("Gas used mismatch: header %d computed %d" %
                             (block.meta.evmGasUsed, evmState.gas_used))

        # The rest fee goes to root block
        if evmState.block_fee // 2 != block.meta.coinbaseAmount:
            raise ValueError("Coinbase reward incorrect")
        # TODO: Check evm receipt and bloom

        # TODO: Add block reward to coinbase
        # self.rewardCalc.getBlockReward(self):
        self.db.putMinorBlock(block, evmState)

        # Update tip if a block is appended or a fork is longer (with the same ancestor confirmed by root block tip)
        if block.header.hashPrevMinorBlock == self.headerTip.getHash() or \
                (block.header.height > self.headerTip.height and self.__isMinorBlockLinkedToRootTip(block)):
            self.evmState = evmState
            self.headerTip = block.header
            self.metaTip = block.meta

        return None

    def getTip(self):
        return self.db.getMinorBlockByHash(self.headerTip.getHash())

    def getBlockHeaderByHeight(self, height):
        pass

    def getBalance(self, recipient):
        return self.evmState.get_balance(recipient)

    def getNextBlockDifficulty(self, createTime):
        return self.diffCalc.calculateDiff(self, createTime)

    def getNextBlockReward(self):
        return self.rewardCalc.getBlockReward(self)

    def createBlockToAppend(self, createTime=None, address=None):
        """ Create an empty block to append
        """
        block = self.tip().createBlockToAppend(
            createTime=createTime,
            address=address,
            quarkash=self.getNextBlockReward())
        block.header.difficulty = self.getNextBlockDifficulty(block.header.createTime)
        return block

    def createBlockToMine(self, createTime=None, address=None, includeTx=True):
        """ Create a block to append and include TXs to maximize rewards
        """
        pass

    def addTransactionToQueue(self, transaction):
        # TODO: limit transaction queue size
        self.transactionPool.add(transaction, self.utxoPool)

    def getPendingTxSize(self):
        return self.transactionPool.size()

    #
    # ============================ Cross-shard transaction handling =============================
    #
    def addCrossShardTxListByMinorBlockHash(self, h, txList):
        ''' Add a cross shard tx list from remote shard
        The list should be validated by remote shard, however,
        it is better to diagnose some bugs in peer shard if we could check
        - x-shard gas limit exceeded
        - it is a neighor of current shard following our routing rule
        '''
        self.db.putMinorBlockXshardTxList(h, txList)

    def addRootBlock(self, rBlock):
        ''' Add a root block.
        Make sure all cross shard tx lists of remote shards confirmed by the root block are in local db.
        '''
        if not self.db.containRootBlockByHash(rBlock.header.hashPrevBlock):
            raise ValueError("cannot find previous root block in pool")

        shardHeader = None
        for mHeader in rBlock.minorBlockHeaderList:
            h = mHeader.getHash()
            if mHeader.branch == self.branch:
                if not self.db.containMinorBlockByHash(h):
                    raise ValueError("cannot find minor block in local shard")
                if shardHeader is None or shardHeader.height < mHeader.header:
                    shardHeader = mHeader
                continue

            if not self.__isNeighbor(mHeader.branch):
                continue

            if not self.db.containRemoteMinorBlockHash(h):
                raise ValueError("cannot find xShard tx list")

        self.db.putRootBlock(rBlock)

        if rBlock.header.height > self.rootTip.height:
            # Switch to the longest root block
            # shardHeader cannot be None since PROOF_OF_PROGRESS should be positive
            self.rootTip = rBlock.header
            self.headerTip = shardHeader
            self.metaTip = self.db.getMinorBlockMetaByHash(self.headerTip.getHash())
            # TODO: Should search and set the longest one linked to the root
            self.confirmedHeaderTip = self.headerTip
            self.confirmedMetaTip = self.metaTip
            return True
        return False

    def __isNeighbor(self, remoteBranch):
        # TODO: Apply routing rule to determine neighors that could directly send x-shard tx
        return True

    def __getCrossShardTxListByRootBlockHash(self, h):
        rBlock = self.db.getRootBlockByHash(h)
        txList = []
        for mHeader in rBlock.minorBlockHeaderList:
            if mHeader.branch == self.branch:
                continue

            if not self.__isNeighbor(mHeader.branch):
                continue

            h = mHeader.getHash()
            txList.extend(self.db.getMinorBlockXshardTxList(h).txList)

        # Apply root block coinbase
        if self.branch.isInShard(rBlock.header.coinbaseAddress.fullShardId):
            txList.append(CrossShardTransactionDeposit(
                address=rBlock.header.coinbaseAddress,
                amount=rBlock.header.coinbaseAmount,
                gasPrice=0))
        return txList

    def __runCrossShardTxList(self, evmState, descendantRootHeader, ancestorRootHeader):
        rHeader = descendantRootHeader
        while rHeader != ancestorRootHeader:
            if rHeader.height == ancestorRootHeader.height:
                raise ValueError(
                    "incorrect ancestor root header: expected {}, actual {}",
                    rHeader.getHash().hex(),
                    ancestorRootHeader.getHash().hex())
            if evmState.gas_used == evmState.gas_limit:
                raise ValueError("gas consumed by cross-shard tx exceeding limit")

            txList = self.__getCrossShardTxListByRootBlockHash(descendantRootHeader.getHash())
            for tx in txList:
                evmState.delta_balance(tx.address.recipient, tx.amount)
                evmState.gas_used = min(evmState.gas_used + opcodes.GTXXSHARDCOST, evmState.gas_limit)
                evmState.block_fee += opcodes.GTXXSHARDCOST * tx.gasPrice
                evmState.delta_balance(evmState.block_coinbase, opcodes.GTXXSHARDCOST * tx.gasPrice)

            rHeader = self.db.getRootBlockHeaderByHash(rHeader.hashPrevBlock)

            # TODO: Check x-shard gas used is within limit
            # TODO: Refill local x-shard gas
