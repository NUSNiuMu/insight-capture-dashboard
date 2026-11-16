"""Browser-origin policy must stop the handler, without requiring native auth."""
import asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient,TestServer
import pytest
from insight_capture.api.middleware import create_browser_write_origin_middleware

@pytest.mark.parametrize('method',['POST','PUT','PATCH','DELETE'])
@pytest.mark.parametrize('origin',['http://another-device:8765','null',''])
def test_foreign_browser_write_never_reaches_handler(method,origin):
    async def scenario():
        calls=[]
        async def mutate(request):
            calls.append(request.method)
            return web.json_response({'ok':True})
        app=web.Application(middlewares=[create_browser_write_origin_middleware()])
        app.router.add_route('*','/api/fixture',mutate)
        async with TestClient(TestServer(app)) as client:
            response=await client.request(method,'/api/fixture',headers={'Origin':origin,'Content-Type':'text/plain'},data='{}')
            assert response.status==403
            assert (await response.json())['error']=='cross_origin_request'
            assert calls==[]
    asyncio.run(scenario())

@pytest.mark.parametrize('native',[False,True])
def test_device_page_and_native_client_remain_usable(native):
    async def scenario():
        calls=[]
        async def mutate(request):
            calls.append(request.method)
            return web.json_response({'ok':True})
        app=web.Application(middlewares=[create_browser_write_origin_middleware()])
        app.router.add_post('/api/fixture',mutate)
        async with TestClient(TestServer(app)) as client:
            headers={} if native else {'Origin':str(client.make_url('')).rstrip('/')}
            response=await client.post('/api/fixture',headers=headers,json={})
            assert response.status==200
            assert (await response.json())['ok']
            assert calls==['POST']
    asyncio.run(scenario())

@pytest.mark.parametrize('method',['GET','HEAD','OPTIONS'])
def test_read_and_preflight_methods_are_not_reclassified_as_writes(method):
    async def scenario():
        async def handle(_):return web.Response(status=204)
        app=web.Application(middlewares=[create_browser_write_origin_middleware()])
        app.router.add_route('*','/api/fixture',handle)
        async with TestClient(TestServer(app)) as client:
            response=await client.request(method,'/api/fixture',headers={'Origin':'http://another-device:8765'})
            assert response.status==204
            assert 'Access-Control-Allow-Origin' not in response.headers
    asyncio.run(scenario())
