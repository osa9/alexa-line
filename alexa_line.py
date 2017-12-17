# coding: UTF-8

import logging
import boto3
import os
import json
import time
import requests

from linebot import (
    LineBotApi, WebhookHandler
)

from linebot.exceptions import (
    InvalidSignatureError
)

from linebot.models import (
    MessageEvent, PostbackEvent,
    TextMessage,
    TemplateSendMessage, ConfirmTemplate,
    TextSendMessage,
    PostbackTemplateAction
)

from linebot.models.sources import (
    SourceGroup, SourceRoom
)


# Messaging APIのBot情報を入れる
LINE_CHANNEL_SECRET = 'XXX'
LINE_ACCESS_TOKEN = 'XXX'

# LINEでinfoって打って調べた後，再度デプロイする
LINE_ROOM_ID = 'XXX'

DYNAMODB_TABLE = os.environ['DYNAMODB_TABLE']


linebot = LineBotApi(LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

logger = logging.getLogger('alexa-line')
logger.setLevel(logging.INFO)


# DynamoDBに会話情報を保存する
def create_session(hash_key, message):
    db = boto3.resource('dynamodb').Table(DYNAMODB_TABLE)
    db.put_item(Item={
        'id': hash_key,
        'status': 'created',
        'message': message
    })


# DynamoDBに応答を保存する
def update_session(hash_key, status, reply_message):
    db = boto3.resource('dynamodb').Table(DYNAMODB_TABLE)
    db.update_item(
        Key={'id': hash_key},
        AttributeUpdates={
            'status': {
                'Value': status,
                'Action': 'PUT'
            },
            'reply_message': {
                'Value': reply_message,
                'Action': 'PUT'
            }
        })


# 会話を取得する
def get_session(hash_key):
    db = boto3.resource('dynamodb').Table(DYNAMODB_TABLE)
    res = db.get_item(Key={'id': hash_key})
    return res.get('Item', None)


# 応答までポーリングする(デフォルトタイムアウトは30秒)
def polling_session(hash_key, interval=5, retries=6):
    for retry_count in range(retries):
        item = get_session(hash_key)
        if item and item['status'] == 'replied':
            return item
        time.sleep(interval)
    return None

# API Gatewayのレスポンスを作成
def http_response(code, body, res=None):
    return {
        'statusCode': code,
        'body': json.dumps(body, ensure_ascii=False),
        'headers': {
            'Content-Type': 'application/json',
        },
    }


# LINEでメッセージが来た時
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    if event.message.text == 'info':
        source = event.source

        message = 'UserId={}'.format(source.user_id)

        if isinstance(source, SourceGroup):
            message += ', GroupId={}'.format(source.group_id)
        elif isinstance(source, SourceRoom):
            message += ', RoomId={}'.format(source.room_id)

        linebot.reply_message(
            event.reply_token,
            TextSendMessage(text=message))


# LINEでボタンが押された時
@handler.add(PostbackEvent)
def handle_postback(event):
    data = json.loads(event.postback.data)
    print('DataRecieve: {}'.format(data))

    update_session(data['id'], 'replied', data['message'])


# LINEのWebHookイベント
# ボタンが押されたらDBにメッセージをセットする
def line_endpoint(event, context):
    logger.info(event)
    body = event['body']
    signature = event['headers']['X-Line-Signature']

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.error("Invalid Signature")
        return http_response(400, {'status': 'error', 'message': 'Invalid Signature'})

    return http_response(200, {'status': 'ok'})


# LINEでメッセージを送る
def line_send_message(to, key, message):
    # message [はい][いいえ] みたいな感じのメッセージ
    line_message = TemplateSendMessage(
        alt_text=message,
        template=ConfirmTemplate(
            text=message,
            actions=[
                PostbackTemplateAction(
                    label='はい',
                    text='はい',
                    data=json.dumps({'id': key, 'message': 'はい'})
                ),
                PostbackTemplateAction(
                    label='いいえ',
                    text='いいえ',
                    data=json.dumps({'id': key, 'message': 'いいえ'})
                )
            ]
        )
    )

    linebot.push_message(to, line_message)


# Progressive Responseを送信
# 参考: https://developer.amazon.com/ja/docs/custom-skills/send-the-user-a-progressive-response.html
def send_progressive_response(event, message):
    endpoint = event['context']['System'].get('apiEndpoint')
    access_token =  event['context']['System'].get('apiAccessToken')
    request_id = event['request']['requestId']

    # エンドポイント情報が無い時(シミュレーター等)
    if not endpoint or not access_token:
        logger.warn('No Endpoint')
        return

    response = {
        "header": {
            "requestId": request_id
        },
        "directive": {
            "type": "VoicePlayer.Speak",
            "speech": message
        }
    }

    logger.info(response)

    res = requests.post(
        endpoint + '/v1/directives',
        headers={
            'Authorization': 'Bearer {}'.format(access_token),
            'Content-Type': 'application/json'
        },
        data=json.dumps(response))

    if res.status_code != 200:
        logger.error('Progressive Response Failed (status_code={})'
                     .format(res.status_code))


def handle_message_intent(sessionId, event):
    intent = event['request']['intent']
    message = intent['slots']['Message']['value']
    create_session(sessionId, message)
    line_send_message(LINE_ROOM_ID, sessionId, message)


# Alexaから呼ばれた時
def alexa_endpoint(event, context):
    logger.info(event)

    sessionId = event['session']['sessionId']

    # LINEでメッセージを送信する
    request = event['request']
    if request['type'] == "IntentRequest":
        intent = request['intent']
        if intent['name'] == 'SendMessageIntent':
            handle_message_intent(event)

    # Alexaに経過を報告する
    send_progressive_response(event, 'メッセージを送信しました．応答待ちです．')

    # LINEで応答が来るまで待つ(30秒)
    result = polling_session(sessionId)

    if result:
        message = result['reply_message']
    else:
        message = '応答がありませんでした'

    # Alexaに応答を返す
    response = {
        'version': '1.0',
        'response': {
            'outputSpeech': {
                'type': 'PlainText',
                'text': message,
            }
        }
    }

    return response
