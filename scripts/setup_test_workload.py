#!/usr/bin/env python3
"""
Deploy a disposable test workload on minikube so node_remediator pre-flight
checks (available replicas, PDB) have something safe to act on.

Creates:
    namespace:  atoloan-test
    deployment: test-api (nginx:alpine, 2 replicas)

Usage:
    python scripts/setup_test_workload.py
"""

import asyncio

from kubernetes_asyncio import client, config
from kubernetes_asyncio.client import ApiException

NAMESPACE = "atoloan-test"
DEPLOYMENT = "test-api"


async def main() -> None:
    await config.load_kube_config()
    async with client.ApiClient() as api:
        v1 = client.CoreV1Api(api)
        apps_v1 = client.AppsV1Api(api)

        try:
            await v1.create_namespace(
                client.V1Namespace(metadata=client.V1ObjectMeta(name=NAMESPACE))
            )
            print(f"Created namespace '{NAMESPACE}'")
        except ApiException as exc:
            if exc.status == 409:
                print(f"Namespace '{NAMESPACE}' already exists")
            else:
                raise

        deployment = client.V1Deployment(
            metadata=client.V1ObjectMeta(name=DEPLOYMENT, namespace=NAMESPACE),
            spec=client.V1DeploymentSpec(
                replicas=2,
                selector=client.V1LabelSelector(match_labels={"app": DEPLOYMENT}),
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(labels={"app": DEPLOYMENT}),
                    spec=client.V1PodSpec(
                        containers=[
                            client.V1Container(
                                name=DEPLOYMENT,
                                image="nginx:alpine",
                                resources=client.V1ResourceRequirements(
                                    requests={"memory": "32Mi", "cpu": "50m"},
                                ),
                            )
                        ],
                    ),
                ),
            ),
        )
        try:
            await apps_v1.create_namespaced_deployment(NAMESPACE, deployment)
            print(f"Created deployment '{NAMESPACE}/{DEPLOYMENT}' (2 replicas)")
        except ApiException as exc:
            if exc.status == 409:
                print(f"Deployment '{NAMESPACE}/{DEPLOYMENT}' already exists")
            else:
                raise


if __name__ == "__main__":
    asyncio.run(main())
