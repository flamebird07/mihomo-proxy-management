import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[1]/'scripts'))
import source1_independent as s1


def test_status_not_delay_controls_success():
    assert not s1.accepted('grok', {'exit':0, 'status':403, 'ms':500})
    assert s1.accepted('chatgpt', {'exit':0, 'status':200, 'ms':500})
    assert not s1.accepted('chatgpt', {'exit':0, 'status':421})
    assert s1.accepted('openai', {'exit':0, 'status':421})
    assert not s1.accepted('grok', {'exit':28, 'status':200})


def test_live_gpt_with_blocked_grok_has_valid_general_delay():
    fields=s1.latency_fields(None, 750, 1006)
    assert fields['存活'] is True
    assert fields['延迟ms']==750
    assert fields['Grok延迟ms']==-1


def test_all_failed_and_numeric_strings():
    fields=s1.latency_fields('-1', None, '-1')
    assert fields['存活'] is False and fields['延迟ms']==-1
    assert s1.latency_fields('900', '700', '800')['延迟ms']==700


def test_scoped_replace_preserves_other_sources():
    class Client:
        def __init__(self):
            self.rows = [{'record_id':'other', 'fields':{'节点名':'same','测速路径':'源3独立兼容内核'}},
                         {'record_id':'old', 'fields':{'节点名':'same','测速路径':s1.LABEL}}]
        def list_records(self, table):return self.rows[:]
        def batch_create(self, table, records):
            self.rows += [{'record_id':str(i),**r} for i,r in enumerate(records)]
        def batch_delete(self, table, ids):
            self.rows=[r for r in self.rows if r['record_id'] not in ids]
    c=Client()
    result=s1.replace_scope(c,'table',[{'fields':{'节点名':'new','测速路径':s1.LABEL,'存活':True,'同步时间':123}}])
    assert len(result)==1
    assert any(r['record_id']=='other' for r in c.rows)
    assert not any(r['record_id']=='old' for r in c.rows)
