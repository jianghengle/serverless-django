import ipaddress
import json
import os
import re
import boto3
import time
import requests
from botocore.exceptions import ClientError


_ec2_client = None


def _ec2():
    global _ec2_client
    if _ec2_client is None:
        _ec2_client = boto3.client('ec2')
    return _ec2_client


def handle_ip_allocation(event, context):
    """Allocate an Elastic IP for a dev box and return its allocation id.

    Input:  {"name": "<stable box name>"}  (optional but recommended)
    Output: {"allocationId": "eipalloc-...", "publicIp": "1.2.3.4", "hostname": "1-2-3-4.sslip.io"}

    If `name` is given, the address is tagged DevBox=<name>, and a retry (for example a Step Functions
    retry) returns the address already allocated for that name instead of allocating, and paying for,
    a second one.
    """
    name = (event or {}).get('name')
    if name is not None and not (isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9._-]{1,64}', name)):
        raise ValueError('name must be 1-64 characters of letters, digits, ".", "_" or "-"')

    ec2 = _ec2()
    if name:
        existing = ec2.describe_addresses(Filters=[{'Name': 'tag:DevBox', 'Values': [name]}])['Addresses']
        if existing:
            return _ip_result(existing[0]['AllocationId'], existing[0]['PublicIp'])

    tags = [{'Key': 'Name', 'Value': f'dev-box-{name}' if name else 'dev-box'}]
    if name:
        tags.append({'Key': 'DevBox', 'Value': name})
    resp = ec2.allocate_address(
        Domain='vpc',
        TagSpecifications=[{'ResourceType': 'elastic-ip', 'Tags': tags}])
    return _ip_result(resp['AllocationId'], resp['PublicIp'])


def _ip_result(allocation_id, public_ip):
    return {
        'allocationId': allocation_id,
        'publicIp': public_ip,
        'hostname': public_ip.replace('.', '-') + '.sslip.io',
    }


USER_DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'user-data.sh')
ALLOCATION_ID_RE = re.compile(r'eipalloc-[0-9a-f]{8,17}')
# Both values are written into a shell script as double-quoted strings, so accept only plain URL characters.
API_SERVER_RE = re.compile(r'https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?')


def _render_user_data(allocation_id, prod_api_server):
    """Fill ALLOC_ID and PROD_API_SERVER into user-data.sh."""
    with open(USER_DATA_PATH, encoding='utf-8') as f:
        script = f.read()
    for var, value in (('ALLOC_ID', allocation_id), ('PROD_API_SERVER', prod_api_server)):
        script, count = re.subn(rf'^{var}=".*"$', lambda m: f'{var}="{value}"', script, count=1, flags=re.M)
        if count != 1:
            raise RuntimeError(f'user-data.sh has no {var}="..." line to fill in')
    return script


def _default_vpc_id(ec2):
    vpcs = ec2.describe_vpcs(Filters=[{'Name': 'isDefault', 'Values': ['true']}])['Vpcs']
    if not vpcs:
        raise RuntimeError('No default VPC in this region')
    return vpcs[0]['VpcId']


def _ensure_security_group(ec2, vpc_id, allocation_id, tags, ssh_cidr):
    """Security group: 80 and 443 from anywhere, 22 only from ssh_cidr. Reuses the group on a retry."""
    group_name = f'dev-box-{allocation_id}'
    groups = ec2.describe_security_groups(Filters=[
        {'Name': 'group-name', 'Values': [group_name]},
        {'Name': 'vpc-id', 'Values': [vpc_id]},
    ])['SecurityGroups']
    if groups:
        group_id = groups[0]['GroupId']
    else:
        group_id = ec2.create_security_group(
            GroupName=group_name, Description='Dev box: HTTP, HTTPS and SSH', VpcId=vpc_id,
            TagSpecifications=[{'ResourceType': 'security-group', 'Tags': tags}])['GroupId']
    rules = [
        (80, '0.0.0.0/0', 'HTTP (certificate issuance, redirect)'),
        (443, '0.0.0.0/0', 'HTTPS'),
        (22, ssh_cidr, 'SSH'),
    ]
    for port, cidr, label in rules:
        try:
            ec2.authorize_security_group_ingress(GroupId=group_id, IpPermissions=[
                {'IpProtocol': 'tcp', 'FromPort': port, 'ToPort': port,
                 'IpRanges': [{'CidrIp': cidr, 'Description': label}]}])
        except ClientError as e:
            if e.response['Error']['Code'] != 'InvalidPermission.Duplicate':
                raise
    return group_id


def _validate_ssh_cidr(value):
    """An IPv4 CIDR such as 203.0.113.7/32. Returns it normalized."""
    try:
        network = ipaddress.ip_network(value, strict=True)
    except (ValueError, TypeError):
        raise ValueError('SSH_CIDR must be an IPv4 CIDR such as 203.0.113.7/32') from None
    if network.version != 4:
        raise ValueError('SSH_CIDR must be an IPv4 CIDR')
    return str(network)


def _root_volume(ec2, ami_id, size_gb):
    """Root EBS mapping for the AMI's own root device, resized to size_gb. Also checks the AMI is arm64."""
    images = ec2.describe_images(ImageIds=[ami_id])['Images']
    if not images:
        raise RuntimeError(f'AMI {ami_id} not found')
    image = images[0]
    if image.get('Architecture') != 'arm64':
        raise RuntimeError(f'AMI {ami_id} is {image.get("Architecture")}, but t4g instances need arm64')
    root = image['RootDeviceName']
    for mapping in image.get('BlockDeviceMappings', []):
        current = mapping.get('Ebs', {}).get('VolumeSize')
        if mapping['DeviceName'] == root and current and current > size_gb:
            raise RuntimeError(f'AMI root volume is {current} GB, larger than the requested {size_gb} GB')
    return [{'DeviceName': root,
             'Ebs': {'VolumeSize': size_gb, 'VolumeType': 'gp3', 'DeleteOnTermination': True}}]


def handle_ec2_creation(event, context):
    """Launch the dev box instance.

    Input:  {"allocationId": "eipalloc-...", "prodApiServer": "https://...", "name": "<box name>" (optional)}
            (the output of handle_ip_allocation plus prodApiServer; extra fields are passed through)
    Output: the input plus {"instanceId", "securityGroupId"}. The instance is still starting; the user-data
            script attaches the Elastic IP, then configures Caddy and the dev-box files.

    Safe to retry: the instance uses the allocation id as its idempotency token, and the security group
    is looked up by name before it is created.
    """
    event = event or {}
    allocation_id = event.get('allocationId')
    prod_api_server = event.get('prodApiServer')
    name = event.get('name')
    if not isinstance(allocation_id, str) or not ALLOCATION_ID_RE.fullmatch(allocation_id):
        raise ValueError('allocationId must look like eipalloc-0123456789abcdef0')
    if not isinstance(prod_api_server, str) or not API_SERVER_RE.fullmatch(prod_api_server):
        raise ValueError('prodApiServer must be an https:// URL')
    if name is not None and not (isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9._-]{1,64}', name)):
        raise ValueError('name must be 1-64 characters of letters, digits, ".", "_" or "-"')

    ami_id = os.environ['AMI_ID']
    key_name = os.environ['KEY_NAME']
    instance_type = os.environ['INSTANCE_TYPE']
    volume_gb = int(os.environ['VOLUME_SIZE_GB'])
    instance_profile = os.environ['INSTANCE_PROFILE_NAME']
    ssh_cidr = _validate_ssh_cidr(os.environ.get('SSH_CIDR'))

    tags = [{'Key': 'Name', 'Value': f'dev-box-{name}' if name else f'dev-box-{allocation_id}'}]
    if name:
        tags.append({'Key': 'DevBox', 'Value': name})

    user_data = _render_user_data(allocation_id, prod_api_server)
    ec2 = _ec2()
    security_group_id = _ensure_security_group(ec2, _default_vpc_id(ec2), allocation_id, tags, ssh_cidr)
    resp = ec2.run_instances(
        ImageId=ami_id,
        InstanceType=instance_type,
        KeyName=key_name,
        MinCount=1, MaxCount=1,
        SecurityGroupIds=[security_group_id],
        IamInstanceProfile={'Name': instance_profile},
        BlockDeviceMappings=_root_volume(ec2, ami_id, volume_gb),
        UserData=user_data,  # boto3 base64-encodes this
        # IMDSv2 only. Hop limit 1 keeps containers on the box from reaching the instance credentials.
        MetadataOptions={'HttpTokens': 'required', 'HttpEndpoint': 'enabled', 'HttpPutResponseHopLimit': 1},
        ClientToken=allocation_id,
        TagSpecifications=[{'ResourceType': rt, 'Tags': tags} for rt in ('instance', 'volume')],
    )
    return {**event, 'instanceId': resp['Instances'][0]['InstanceId'], 'securityGroupId': security_group_id}


HOSTNAME_RE = re.compile(r'[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?')
REQUEST_TIMEOUT = 10


def handle_check_ec2(event, context):
    """Check whether the new dev box is serving, and if so report it to the prod API.

    Input:  {"hostname", "token", "repo", "prodApiServer", "instanceId"}
    Output: True once the box answered 200 and the prod API was updated, otherwise False
            (the Step Function calls this repeatedly until it returns True).
    """
    event = event or {}
    hostname = event.get('hostname')
    token = event.get('token')
    repo = event.get('repo')
    prod_api_server = event.get('prodApiServer')
    instance_id = event.get('instanceId')
    if not isinstance(hostname, str) or not HOSTNAME_RE.fullmatch(hostname):
        raise ValueError('hostname must be a plain host name')
    if not isinstance(token, str) or not token or not isinstance(repo, str) or not repo:
        raise ValueError('token and repo are required')
    if not isinstance(prod_api_server, str) or not API_SERVER_RE.fullmatch(prod_api_server):
        raise ValueError('prodApiServer must be an https:// URL')

    url = f'https://{hostname}'
    # The box is expected to be unreachable while it boots, installs its certificate, and starts the
    # dev-box server, so connection, TLS and timeout errors just mean "not ready yet". TLS verification
    # stays on: an invalid certificate is also "not ready".
    try:
        resp = requests.post(f'{url}/dev-box/status', json={'token': token, 'repo': repo},
                             timeout=REQUEST_TIMEOUT)
    except requests.RequestException:
        return False
    if resp.status_code != 200:
        return False

    try:
        resp = requests.post(
            f'{prod_api_server.rstrip("/")}/Prod/api/admin/update-dev-box-info',
            json={'status': 'Setting up', 'url': url, 'instanceId': instance_id},
            headers={'Authorization': token},
            timeout=REQUEST_TIMEOUT)
    except requests.RequestException:
        return False
    return resp.status_code == 200


SERVICE_KEYS = ('frontendService', 'backendService', 'codingAgentService')
STATUS_POLL_SECONDS = 10
SETUP_REQUEST_MAX_SECONDS = 600  # the box's setup call may clone the repo, which can take minutes
SAFETY_MARGIN_SECONDS = 30       # stop waiting this long before the Lambda timeout, so we can fail cleanly


def handle_dev_box_setup(event, context):
    """Set up the dev box (clone the repo, start the services), wait for it, then mark it Ready.

    Input:  {"hostname", "token", "repo", "gitBucket", "prodApiServer"}
    Output: {"status": "Ready", "url": "https://<hostname>"}
    Raises if setup fails, a service fails or is missing, the wait runs out of time, or the final
    update is rejected, so the Step Function can retry or catch. It is safe to run again: the box's
    setup endpoint skips an existing clone and only restarts services whose directory changed.
    """
    event = event or {}
    hostname = event.get('hostname')
    token = event.get('token')
    repo = event.get('repo')
    git_bucket = event.get('gitBucket')
    prod_api_server = event.get('prodApiServer')
    if not isinstance(hostname, str) or not HOSTNAME_RE.fullmatch(hostname):
        raise ValueError('hostname must be a plain host name')
    for field, value in (('token', token), ('repo', repo), ('gitBucket', git_bucket)):
        if not isinstance(value, str) or not value:
            raise ValueError(f'{field} is required')
    if not isinstance(prod_api_server, str) or not API_SERVER_RE.fullmatch(prod_api_server):
        raise ValueError('prodApiServer must be an https:// URL')

    def seconds_left():
        return context.get_remaining_time_in_millis() / 1000 - SAFETY_MARGIN_SECONDS

    url = f'https://{hostname}'

    # 1. Ask the box to set itself up.
    try:
        resp = requests.post(f'{url}/dev-box/setup',
                             json={'token': token, 'repo': repo, 'gitBucket': git_bucket},
                             timeout=(REQUEST_TIMEOUT, max(REQUEST_TIMEOUT, min(SETUP_REQUEST_MAX_SECONDS, seconds_left()))))
    except requests.RequestException as e:
        raise RuntimeError(f'dev-box/setup request failed: {type(e).__name__}') from None
    if resp.status_code != 200:
        raise RuntimeError(f'dev-box/setup returned {resp.status_code}: {resp.text[:1000]}')

    # 2. Wait until all three services are active.
    last = {}
    while True:
        try:
            resp = requests.post(f'{url}/dev-box/status', json={'token': token, 'repo': repo},
                                 timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                last = resp.json()
        except (requests.RequestException, ValueError):
            pass  # transient; keep polling
        states = {k: last.get(k) for k in SERVICE_KEYS}
        if all(v == 'active' for v in states.values()):
            break
        # These do not fix themselves (the units have no Restart=), so fail fast instead of waiting.
        broken = {k: v for k, v in states.items() if v in ('failed', 'not-installed')}
        if broken:
            raise RuntimeError(f'Dev box services cannot start: {broken}')
        if seconds_left() < STATUS_POLL_SECONDS + REQUEST_TIMEOUT:
            raise RuntimeError(f'Timed out waiting for services to become active: {states}')
        time.sleep(STATUS_POLL_SECONDS)

    # 3. Tell the prod API the box is ready.
    try:
        resp = requests.post(f'{prod_api_server.rstrip("/")}/Prod/api/admin/update-dev-box-info',
                             json={'status': 'Ready'}, headers={'Authorization': token},
                             timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        raise RuntimeError(f'update-dev-box-info request failed: {type(e).__name__}') from None
    if resp.status_code != 200:
        raise RuntimeError(f'update-dev-box-info returned {resp.status_code}')
    return {'status': 'Ready', 'url': url}


INSTANCE_ID_RE = re.compile(r'i-[0-9a-f]{8,17}')
SSLIP_HOST_RE = re.compile(r'(\d{1,3})-(\d{1,3})-(\d{1,3})-(\d{1,3})\.sslip\.io')
OWNED_NAME_PREFIX = 'dev-box-'  # every resource this launcher creates has Name=dev-box-...


def _is_ours(tags):
    return any(t['Key'] == 'Name' and t['Value'].startswith(OWNED_NAME_PREFIX) for t in tags or [])


def _error_code(e):
    return e.response.get('Error', {}).get('Code')


def handle_dev_box_tear_down(event, context):
    """Tear down a dev box: mark it Terminating, terminate the instance, delete its security group,
    and release its Elastic IP.

    Input:  {"token", "prodApiServer", "instanceId", "devBoxUrl"}
    Output: {"instanceId", "securityGroupsDeleted": [...], "addressesReleased": [...]}
    Finally posts {status: "Terminated", instanceId: null, url: null} to the prod API.

    Only resources this launcher created (Name tag starting "dev-box-") are touched, so a wrong id cannot
    remove something else. Safe to run again after a partial failure: steps that are already done are skipped.
    """
    event = event or {}
    token = event.get('token')
    prod_api_server = event.get('prodApiServer')
    instance_id = event.get('instanceId')
    dev_box_url = event.get('devBoxUrl')
    if not isinstance(token, str) or not token:
        raise ValueError('token is required')
    if not isinstance(prod_api_server, str) or not API_SERVER_RE.fullmatch(prod_api_server):
        raise ValueError('prodApiServer must be an https:// URL')
    if not isinstance(instance_id, str) or not INSTANCE_ID_RE.fullmatch(instance_id):
        raise ValueError('instanceId must look like i-0123456789abcdef0')
    if not isinstance(dev_box_url, str):
        raise ValueError('devBoxUrl is required')

    # 1. Tell the prod API first. If this is rejected (for example the token is not an admin's),
    #    stop before anything is destroyed.
    try:
        _update_dev_box_info(prod_api_server, token, {'status': 'Terminating'})
    except RuntimeError as e:
        raise RuntimeError(f'{e}; nothing was terminated') from None

    ec2 = _ec2()

    # 2. Find what belongs to this box, and refuse anything that is not ours.
    security_group_ids = []
    try:
        reservations = ec2.describe_instances(InstanceIds=[instance_id])['Reservations']
        instance = reservations[0]['Instances'][0] if reservations else None
    except ClientError as e:
        if _error_code(e) != 'InvalidInstanceID.NotFound':
            raise
        instance = None  # already gone; clean up whatever else we can identify
    if instance is not None:
        if not _is_ours(instance.get('Tags')):
            raise RuntimeError(f'{instance_id} was not created by the dev box launcher; refusing to terminate it')
        security_group_ids = [g['GroupId'] for g in instance.get('SecurityGroups', [])]

    allocations = {}  # allocation id -> public ip
    lookups = [{'Filters': [{'Name': 'instance-id', 'Values': [instance_id]}]}]
    host = re.fullmatch(r'https://([^/:]+)/?', dev_box_url)
    sslip = SSLIP_HOST_RE.fullmatch(host.group(1)) if host else None
    if sslip:
        # Covers a box whose Elastic IP was never attached (the user-data script did not get that far).
        lookups.append({'PublicIps': ['.'.join(sslip.groups())]})
    for lookup in lookups:
        try:
            addresses = ec2.describe_addresses(**lookup)['Addresses']
        except ClientError as e:
            if _error_code(e) in ('InvalidAddress.NotFound', 'InvalidParameterValue'):
                continue  # not an Elastic IP of this account any more
            raise
        for a in addresses:
            attached_elsewhere = a.get('InstanceId') and a['InstanceId'] != instance_id
            if _is_ours(a.get('Tags')) and not attached_elsewhere:
                allocations[a['AllocationId']] = a['PublicIp']

    # 3. Terminate the instance and wait; the security group and address are in use until it is gone.
    if instance is not None:
        ec2.terminate_instances(InstanceIds=[instance_id])
        ec2.get_waiter('instance_terminated').wait(
            InstanceIds=[instance_id], WaiterConfig={'Delay': 5, 'MaxAttempts': 90})

    # 4. Delete the security group(s) created for this box. Network interfaces can take a moment to detach.
    deleted_groups = []
    groups = []
    if security_group_ids:
        try:
            groups = ec2.describe_security_groups(GroupIds=security_group_ids)['SecurityGroups']
        except ClientError as e:
            if _error_code(e) != 'InvalidGroup.NotFound':
                raise  # NotFound: a repeat run after they were deleted
    for group in groups:
        if not (_is_ours(group.get('Tags')) and group['GroupName'].startswith(OWNED_NAME_PREFIX)):
            continue
        for attempt in range(12):
            try:
                ec2.delete_security_group(GroupId=group['GroupId'])
                deleted_groups.append(group['GroupId'])
                break
            except ClientError as e:
                code = _error_code(e)
                if code == 'InvalidGroup.NotFound':
                    break
                if code != 'DependencyViolation' or attempt == 11:
                    raise
                time.sleep(5)

    # 5. Release the Elastic IP(s). The address is free once the terminated instance has let go of it.
    released = []
    for allocation_id, public_ip in allocations.items():
        for attempt in range(12):
            try:
                ec2.release_address(AllocationId=allocation_id)
                released.append(public_ip)
                break
            except ClientError as e:
                code = _error_code(e)
                if code == 'InvalidAllocationID.NotFound':
                    break
                if code not in ('InvalidIPAddress.InUse', 'AuthFailure') or attempt == 11:
                    raise
                time.sleep(5)

    # 6. Everything is gone: clear the box's details in the prod API.
    _update_dev_box_info(prod_api_server, token, {'status': 'Terminated', 'instanceId': None, 'url': None})

    return {'instanceId': instance_id, 'securityGroupsDeleted': deleted_groups, 'addressesReleased': released}


def _update_dev_box_info(prod_api_server, token, payload):
    """POST to the prod API's update-dev-box-info; raises RuntimeError unless it answers 200."""
    try:
        resp = requests.post(f'{prod_api_server.rstrip("/")}/Prod/api/admin/update-dev-box-info',
                             json=payload, headers={'Authorization': token}, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        raise RuntimeError(f'update-dev-box-info request failed: {type(e).__name__}') from None
    if resp.status_code != 200:
        raise RuntimeError(f'update-dev-box-info returned {resp.status_code}')

