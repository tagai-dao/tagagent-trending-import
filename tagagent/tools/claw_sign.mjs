#!/usr/bin/env node
import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'
import { createRequire } from 'node:module'
import { pathToFileURL } from 'node:url'

const CLAW_CHAIN_BY_ID = new Map([
  ['56', 'bsc'],
  ['4663', 'robinhood']
])

function parseArgs(argv) {
  const args = {}
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--wallet-dir' && i + 1 < argv.length) {
      args.walletDir = argv[i + 1]
      i += 1
    }
  }
  return args
}

function readStdin() {
  return new Promise((resolve, reject) => {
    let data = ''
    process.stdin.setEncoding('utf8')
    process.stdin.on('data', chunk => {
      data += chunk
    })
    process.stdin.on('end', () => resolve(data))
    process.stdin.on('error', reject)
  })
}

function stripEnvValue(value) {
  const trimmed = value.trim()
  if (
    (trimmed.startsWith('"') && trimmed.endsWith('"')) ||
    (trimmed.startsWith("'") && trimmed.endsWith("'"))
  ) {
    return trimmed.slice(1, -1)
  }
  return trimmed
}

function loadDotenv(envPath) {
  if (!fs.existsSync(envPath)) return
  const text = fs.readFileSync(envPath, 'utf8')
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trim()
    if (!line || line.startsWith('#') || !line.includes('=')) continue
    const [rawKey, ...rest] = line.replace(/^export\s+/, '').split('=')
    const key = rawKey.trim()
    if (!key || process.env[key] != null) continue
    process.env[key] = stripEnvValue(rest.join('='))
  }
}

function normalizeTx(input) {
  const tx = { ...input }
  // Python serializes large integer fields as strings; ethers requires type to be a number.
  if (tx.type != null) tx.type = Number(bigintField(tx.type, 'type'))
  if (tx.gasLimit == null && tx.gas != null) {
    tx.gasLimit = tx.gas
  }
  delete tx.gas
  return tx
}

function bigintField(value, name) {
  if (value == null || value === '') {
    throw new Error(`missing transaction ${name}`)
  }
  try {
    return BigInt(value)
  } catch {
    throw new Error(`invalid transaction ${name}`)
  }
}

function chainForTx(tx) {
  const chainId = bigintField(tx.chainId, 'chainId')
  const chain = CLAW_CHAIN_BY_ID.get(chainId.toString())
  if (!chain) {
    throw new Error(`unsupported Claw transaction chainId: ${chainId}`)
  }
  return { chain, chainId }
}

function sandboxSigningChain(chain) {
  // Robinhood is EVM-compatible. Claw names its signing curves by wallet
  // family rather than by every EVM network that the curve can sign for.
  return chain === 'robinhood' ? 'ethereum' : chain
}

function assertTransactionShape(tx) {
  for (const field of ['from', 'to', 'nonce', 'gasLimit']) {
    if (!Object.hasOwn(tx, field) || tx[field] == null || tx[field] === '') {
      throw new Error(`missing transaction ${field}`)
    }
  }
  const hasGasPrice = tx.gasPrice != null
  const hasMaxFee = tx.maxFeePerGas != null
  const hasPriorityFee = tx.maxPriorityFeePerGas != null
  if (hasGasPrice === (hasMaxFee || hasPriorityFee)) {
    throw new Error('transaction must contain exactly one complete fee model')
  }
  if (hasMaxFee !== hasPriorityFee) {
    throw new Error('EIP-1559 transaction requires both maxFeePerGas and maxPriorityFeePerGas')
  }
  if (tx.type != null) {
    const type = Number(bigintField(tx.type, 'type'))
    if (![0, 1, 2].includes(type)) {
      throw new Error(`unsupported transaction type: ${tx.type}`)
    }
    if (type === 2 && !hasMaxFee) {
      throw new Error('type 2 transaction requires EIP-1559 fees')
    }
    if (type !== 2 && !hasGasPrice) {
      throw new Error(`type ${type} transaction requires gasPrice`)
    }
    if (type === 0 && tx.accessList != null && tx.accessList.length) {
      throw new Error('legacy transaction cannot contain an accessList')
    }
  }
}

function normalizedAddress(ethers, value) {
  if (value == null) return null
  return ethers.getAddress(value).toLowerCase()
}

function normalizedAccessList(ethers, value) {
  return ethers.accessListify(value || []).map(item => ({
    address: normalizedAddress(ethers, item.address),
    storageKeys: item.storageKeys.map(key => String(key).toLowerCase())
  }))
}

function assertBigintEqual(actual, expected, field) {
  if (actual == null || BigInt(actual) !== bigintField(expected, field)) {
    throw new Error(`signed transaction ${field} mismatch`)
  }
}

function verifySignedTransaction(ethers, rawTx, tx, signerAddress, chainId) {
  if (typeof rawTx !== 'string' || !/^0x[0-9a-f]+$/i.test(rawTx)) {
    throw new Error('Claw signer returned an invalid raw transaction')
  }
  const decoded = ethers.Transaction.from(rawTx)
  const requestedFrom = normalizedAddress(ethers, tx.from)
  const resolvedSigner = normalizedAddress(ethers, signerAddress)
  const recoveredFrom = normalizedAddress(ethers, decoded.from)
  if (requestedFrom !== resolvedSigner || recoveredFrom !== requestedFrom) {
    throw new Error('signed transaction from mismatch')
  }
  if (decoded.chainId !== chainId) {
    throw new Error('signed transaction chainId mismatch')
  }
  if (normalizedAddress(ethers, decoded.to) !== normalizedAddress(ethers, tx.to)) {
    throw new Error('signed transaction to mismatch')
  }
  assertBigintEqual(decoded.value, tx.value ?? 0, 'value')
  assertBigintEqual(decoded.nonce, tx.nonce, 'nonce')
  assertBigintEqual(decoded.gasLimit, tx.gasLimit, 'gasLimit')
  if (ethers.hexlify(decoded.data).toLowerCase() !== ethers.hexlify(tx.data ?? '0x').toLowerCase()) {
    throw new Error('signed transaction data mismatch')
  }

  if (tx.gasPrice != null) {
    assertBigintEqual(decoded.gasPrice, tx.gasPrice, 'gasPrice')
    if (decoded.maxFeePerGas != null || decoded.maxPriorityFeePerGas != null) {
      throw new Error('signed transaction fee model mismatch')
    }
  } else {
    assertBigintEqual(decoded.maxFeePerGas, tx.maxFeePerGas, 'maxFeePerGas')
    assertBigintEqual(
      decoded.maxPriorityFeePerGas,
      tx.maxPriorityFeePerGas,
      'maxPriorityFeePerGas'
    )
    if (decoded.gasPrice != null) {
      throw new Error('signed transaction fee model mismatch')
    }
  }

  const expectedAccessList = normalizedAccessList(ethers, tx.accessList)
  const decodedAccessList = normalizedAccessList(ethers, decoded.accessList)
  if (JSON.stringify(decodedAccessList) !== JSON.stringify(expectedAccessList)) {
    throw new Error('signed transaction accessList mismatch')
  }
  if (tx.type != null) {
    if (decoded.type !== Number(bigintField(tx.type, 'type'))) {
      throw new Error('signed transaction type mismatch')
    }
  } else if (tx.maxFeePerGas != null && decoded.type !== 2) {
    throw new Error('signed transaction inferred type mismatch')
  } else if (tx.accessList != null && decoded.type !== 1) {
    throw new Error('signed transaction inferred type mismatch')
  } else if (tx.gasPrice != null && ![0, 1].includes(decoded.type)) {
    throw new Error('signed transaction inferred type mismatch')
  }

  const txHash = ethers.keccak256(rawTx)
  if (!decoded.hash || decoded.hash.toLowerCase() !== txHash.toLowerCase()) {
    throw new Error('signed transaction hash mismatch')
  }
  return { txHash, from: ethers.getAddress(recoveredFrom) }
}

function errorText(error) {
  const message = error && error.message ? String(error.message) : String(error)
  return message.replace(/\s+/g, ' ').slice(0, 500)
}

async function main() {
  const { walletDir } = parseArgs(process.argv.slice(2))
  if (!walletDir) {
    throw new Error('missing --wallet-dir')
  }
  const resolvedWalletDir = path.resolve(walletDir)
  loadDotenv(path.join(resolvedWalletDir, '.env'))

  const rawInput = await readStdin()
  const input = JSON.parse(rawInput || '{}')
  const tx = normalizeTx(input)
  assertTransactionShape(tx)
  const { chain, chainId } = chainForTx(tx)
  const sandboxChain = sandboxSigningChain(chain)
  const clawUrl = pathToFileURL(path.join(resolvedWalletDir, 'src', 'claw.js')).href
  const claw = await import(clawUrl)
  if (!claw.getClawEthersSigner) {
    throw new Error('Claw wallet explicit-chain signer export not found')
  }

  const signer = await claw.getClawEthersSigner(process.env.TAGAI_PREDICT_RPC, sandboxChain)
  const signerChain = String(signer?.config?.chain || '')
    .trim()
    .toLowerCase()
  if (signerChain !== sandboxChain) {
    throw new Error(`Claw signer sandbox chain mismatch: expected ${sandboxChain}`)
  }
  const signerAddress = signer.getAddress ? await signer.getAddress() : ''
  const rawTx = await signer.signTransaction(tx)
  const walletRequire = createRequire(path.join(resolvedWalletDir, 'package.json'))
  const ethersModule = walletRequire('ethers')
  const ethers = ethersModule.ethers || ethersModule
  if (!ethers.Transaction || !ethers.keccak256 || !ethers.getAddress || !ethers.accessListify) {
    throw new Error('required ethers transaction helpers not found')
  }
  const verified = verifySignedTransaction(ethers, rawTx, tx, signerAddress, chainId)
  process.stdout.write(
    JSON.stringify({ ok: true, rawTx, txHash: verified.txHash, from: verified.from, chain }) + '\n'
  )
}

main().catch(error => {
  const message = errorText(error)
  process.stderr.write(message + '\n')
  process.stdout.write(JSON.stringify({ ok: false, error: message }) + '\n')
  process.exit(1)
})
