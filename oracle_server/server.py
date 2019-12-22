from graphqlclient import GraphQLClient
from tvm_valuetypes import deserialize_boc, Cell
import json
import requests
import codecs
from random import randint
import nacl.signing
from time import sleep


oracle_hub_address = "0:bc2b1afd7b59a288293e2b72d43ed02c50c3421f09c46ac34544e5a3f4b6c152"
delay = 20
last_checked_at = "./last_checked"


def resolve_redirect(host, path):
  r = requests.get('https://%s%s'%(host, path))
  return r.url

client = GraphQLClient(resolve_redirect('testnet.ton.dev', '/graphql'))



def get_oracle_data():
  try:
    with open(last_checked_at, "r") as f:
      s = f.read()
    seqno, tm  = s.split(" ")
    return int(seqno), int(tm)
  except:
    return 0, 0

def set_oracle_data(seqno, tm):
  with open(last_checked_at, "w") as f:
    f.write(str(seqno) + " " + str(tm))


def prepare_oracle_addr(oracle_id):
  return ":%.8d"%oracle_id
  
  
def add_header_to_response(query_id, oracle_id, seqno, response):
  # <b seqno 32 u, op 8 u, oracle_id 32 u,
  header = Cell()
  header.data.put_arbitrary_uint(seqno, 32)
  header.data.put_arbitrary_uint(129, 8)
  header.data.put_arbitrary_uint(oracle_id, 32)
  header.data.put_arbitrary_uint(query_id, 96)
  if header.data.length() + response.data.length() > 1023:
    header.refs.append(response)
  else:
    header.concatenate(response)
  return header

  
  
def sign_message(signing_key, cell):
      # 512 signature len
      # 279 message header len: <b b{1000100} s, wallet_addr addr, 0 Gram, b{00} s,
      if(cell.data.length()+ 512 + 279 > 1023):
        top_cell = Cell()
        top_cell.refs.append(cell)
        cell = top_cell
      signature = signing_key.sign(cell.hash())[:64]
      signed_cell = Cell()
      signed_cell.data.put_arbitrary_uint(int.from_bytes(signature, "big"), 512)
      signed_cell.concatenate(cell)
      return signed_cell

def compose_message(signed_result):
  message_cell = Cell()
  #<b b{1000100} s, hub_addr addr, 0 Gram, b{00} s, 
  message_cell.data.put_arbitrary_uint( 0b1000, 4)
  message_cell.data.put_arbitrary_uint( 0b100, 3) #$10 no anycast
  wc, addr = oracle_hub_address.split(":")
  wc=int(wc)
  message_cell.data.put_arbitrary_int(wc, 8)
  message_cell.data.put_arbitrary_uint(int('0x'+addr, 16), 256)
  message_cell.data.put_arbitrary_uint( 0, 6) # 0Gram = 0b0000, b{00}
  message_cell.concatenate(signed_result)
  return message_cell


def send_boc(boc):
  payload_template = '{"jsonrpc":"2.0", "id": %(request_id)s, "method": "sendboc", "params": ["%(boc)s"]}'
  data = {'request_id':randint(0,2**32), 'boc': codecs.decode(codecs.encode(boc,'base64'),'utf8').replace('\n','')}
  print(payload_template%data)
  r = requests.post('https://toncenter.com/api/test/v1', data=payload_template%data)
  print(r, r.status_code, r.text)

query_template = '''
query {
  messages (filter: {src: {eq: "%(contract_addr)s"}, dst: {eq: "%(oracle_addr)s"}, created_at: {gt:%(last_known)d}},
            orderBy:{ path:"created_at", direction:ASC }) {
    body
    created_at
  }
}
'''

def run_server(oracle_id, oracle_private_key, handler):
  signing_key = nacl.signing.SigningKey(oracle_private_key)
  while True:
    seqno, last_known = get_oracle_data()
    query = query_template % {'contract_addr': oracle_hub_address, 
                              'oracle_addr': prepare_oracle_addr(oracle_id), 
                              'last_known': last_known}
    result = json.loads(client.execute(query))
    if 'data' in result and 'messages' in result['data']:
      if len(result['data']['messages']):
        for m in result['data']['messages']:
          body = m['body']
          created_at = m['created_at']
          body = codecs.decode(codecs.encode(body, 'utf8'), 'base64')
          request = deserialize_boc(body)
          print(request)
          query_id, request.data.data = request.data.data[:96], request.data.data[96:] #TODO bad practice
          print(request)
          result = handler(request)
          result_w_header = add_header_to_response(int.from_bytes(query_id, 'big'), oracle_id, seqno, result)
          seqno += 1
          signed_result = sign_message(signing_key, result_w_header)
          out_msg = compose_message(signed_result)
          print(out_msg)
          send_boc(out_msg.serialize_boc(has_idx=False))
          print(out_msg.serialize_boc(has_idx=False))
          set_oracle_data(seqno, created_at)
          print("Processed query with id %s"%query_id.tobytes())
    sleep(delay)