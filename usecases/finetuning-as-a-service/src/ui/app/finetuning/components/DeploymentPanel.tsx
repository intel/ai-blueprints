'use client';

/**
 * Deployment state for one fine-tuned model: where it is in coming up, how to
 * call it once it is serving, and the kubectl commands to look at it directly.
 *
 * Lifted off the fine-tuning job page. Training and serving are different
 * questions asked at different times -- "did my job converge" and "is my model
 * answering requests" -- and putting both on one page meant neither had a
 * readable shape. Logs are deliberately *not* here; they are a tab of their own,
 * because a 14-row log box in the middle of a status summary pushes everything
 * that matters below the fold.
 */

import React, { useState } from 'react';
import { Alert, Button, Progress, Space, Tag, Typography } from 'antd';
import {
  CheckCircleFilled,
  ClockCircleOutlined,
  CloseCircleFilled,
  CodeOutlined,
  LoadingOutlined,
} from '@ant-design/icons';
import type {
  DeploymentPhase,
  DeploymentStepStatus,
  FineTuningJob,
  ModelDeploymentStatus,
} from '@features/finetuning/types';
import { getFineTunedModelName } from '@features/finetuning/utils';

const { Text } = Typography;

export const DEPLOYMENT_PHASE_TEXT: Record<DeploymentPhase, string> = {
  not_deployed: 'Not Deployed',
  installing: 'Installing',
  downloading: 'Fetching Adapter',
  extracting: 'Downloading Base Model',
  loading: 'Loading Model',
  registering: 'Registering with Gateway',
  ready: 'Serving',
  failed: 'Failed',
  uninstalling: 'Removing',
  unavailable: 'Unavailable',
};

export const DEPLOYMENT_PHASE_COLOR: Record<DeploymentPhase, string> = {
  not_deployed: 'default',
  installing: 'processing',
  downloading: 'processing',
  extracting: 'processing',
  loading: 'processing',
  registering: 'processing',
  ready: 'success',
  failed: 'error',
  uninstalling: 'warning',
  unavailable: 'default',
};

const DEPLOYMENT_STEP_ICON: Record<DeploymentStepStatus, React.ReactNode> = {
  pending: <ClockCircleOutlined style={{ color: '#bfbfbf' }} />,
  active: <LoadingOutlined style={{ color: '#1890ff' }} />,
  done: <CheckCircleFilled style={{ color: '#52c41a' }} />,
  error: <CloseCircleFilled style={{ color: '#ff4d4f' }} />,
};

export interface DeploymentPanelProps {
  job: FineTuningJob;
  deployment?: ModelDeploymentStatus;
}

export default function DeploymentPanel({ job, deployment }: DeploymentPanelProps) {
  const [showCommands, setShowCommands] = useState(false);

  const phase = (deployment?.phase || 'not_deployed') as DeploymentPhase;

  // Deployment names must be RFC 1123 labels, so derive one from the job id. The
  // API derives the same name, so prefer whatever it reports.
  const releaseName =
    deployment?.release_name ||
    `ft-${job.id}`
      .toLowerCase()
      .replace(/[^a-z0-9-]/g, '-')
      .replace(/-+/g, '-')
      .replace(/^-|-$/g, '')
      .slice(0, 43);
  const namespace = deployment?.namespace || 'finetuning-models';

  const servedModelName = deployment?.served_model_name || getFineTunedModelName(job);

  const kubectlCommands = [
    `kubectl get llminferenceservice ${releaseName} -n ${namespace}`,
    `kubectl logs -n ${namespace} -l app.kubernetes.io/name=${releaseName},kserve.io/component=workload -c main -f`,
  ];

  return (
    <Space orientation="vertical" style={{ width: '100%' }} size="middle">
      <div>
        <Space size="middle" style={{ marginBottom: 8 }}>
          <Tag
            color={DEPLOYMENT_PHASE_COLOR[phase] || 'default'}
            style={{ fontSize: '14px', padding: '4px 8px' }}
          >
            {DEPLOYMENT_PHASE_TEXT[phase] || phase}
          </Tag>
          <Text type="secondary">{deployment?.message}</Text>
        </Space>
        {phase !== 'not_deployed' && phase !== 'unavailable' && (
          <Progress
            percent={deployment?.progress ?? 0}
            status={phase === 'failed' ? 'exception' : phase === 'ready' ? 'success' : 'active'}
            showInfo
          />
        )}
      </div>

      {deployment?.error && (
        <Alert title="Deployment Failed" description={deployment.error} type="error" showIcon />
      )}

      {phase === 'unavailable' && (
        <Alert
          title="One-Click Deployment Not Available"
          description="This environment is not set up to deploy models from the UI."
          type="warning"
          showIcon
        />
      )}

      {phase === 'not_deployed' && (
        <Alert
          title="Ready to Deploy"
          description={`Deploying serves your adapter on its base model with the platform's KServe and, once it answers requests, registers it with the LiteLLM gateway as ${servedModelName}. The first deployment of a base model takes several minutes while it is downloaded; registration is the last step, so the gateway only ever lists a model that is ready to use.`}
          type="info"
          showIcon
        />
      )}

      {!!deployment?.steps?.length && (
        <div>
          {deployment.steps.map((step) => (
            <div
              key={step.key}
              style={{ display: 'flex', alignItems: 'flex-start', gap: 8, padding: '4px 0' }}
            >
              <span style={{ lineHeight: '22px' }}>
                {DEPLOYMENT_STEP_ICON[step.status] || DEPLOYMENT_STEP_ICON.pending}
              </span>
              <div>
                <Text
                  type={step.status === 'pending' ? 'secondary' : undefined}
                  strong={step.status === 'active'}
                >
                  {step.title}
                </Text>
                {step.detail && (
                  <div>
                    <Text type="secondary" style={{ fontSize: '12px' }}>
                      {step.detail}
                    </Text>
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {phase === 'ready' && (
        <Alert
          title="Model Is Serving"
          description={
            <Space orientation="vertical" size="small">
              <Text>
                Call it as <Text code copyable>{servedModelName}</Text>
                {deployment?.gateway_registered
                  ? ' through the LiteLLM gateway.'
                  : '. Gateway registration could not be confirmed.'}
              </Text>
              {deployment?.service_url && (
                <Text type="secondary">
                  In-cluster endpoint: <Text code copyable>{deployment.service_url}</Text>
                </Text>
              )}
            </Space>
          }
          type="success"
          showIcon
        />
      )}

      {phase !== 'not_deployed' && phase !== 'unavailable' && (
        <div>
          <Button
            type="link"
            size="small"
            icon={<CodeOutlined />}
            style={{ paddingLeft: 0 }}
            onClick={() => setShowCommands(!showCommands)}
          >
            {showCommands ? 'Hide kubectl commands' : 'Look at it with kubectl'}
          </Button>
          {showCommands && (
            <Space orientation="vertical" size="small" style={{ width: '100%', marginTop: 8 }}>
              <Text type="secondary">
                The model is a KServe LLMInferenceService. From a machine with kubectl access to
                the cluster:
              </Text>
              {kubectlCommands.map((command) => (
                <Text key={command} code copyable>
                  {command}
                </Text>
              ))}
            </Space>
          )}
        </div>
      )}
    </Space>
  );
}
