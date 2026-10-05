import Project from './Project';
import ActionTypes from '../constants/ActionTypes';

const makeProjectClass = () => ({ withId: jest.fn(() => null), upsert: jest.fn() });

const updateHandle = (payload) => ({
  type: ActionTypes.PROJECT_UPDATE_HANDLE,
  payload: { project: { id: 'p-y', name: 'Projeto do grupo Y' }, boardIds: [], ...payload },
});

describe('Project reducer — PROJECT_UPDATE_HANDLE', () => {
  it('não materializa projeto indisponível (evento de outro grupo)', () => {
    const ProjectClass = makeProjectClass();

    Project.reducer(updateHandle({ isAvailable: false }), ProjectClass);

    expect(ProjectClass.upsert).not.toHaveBeenCalled();
  });

  it('remove o projeto local que ficou indisponível sem recriá-lo', () => {
    const projectModel = { deleteWithRelated: jest.fn() };
    const ProjectClass = { withId: jest.fn(() => projectModel), upsert: jest.fn() };

    Project.reducer(updateHandle({ isAvailable: false }), ProjectClass);

    expect(projectModel.deleteWithRelated).toHaveBeenCalledWith(true);
    expect(ProjectClass.upsert).not.toHaveBeenCalled();
  });

  it('atualiza projeto disponível', () => {
    const ProjectClass = makeProjectClass();

    Project.reducer(updateHandle({ isAvailable: true }), ProjectClass);

    expect(ProjectClass.upsert).toHaveBeenCalledWith({ id: 'p-y', name: 'Projeto do grupo Y' });
  });

  it('mantém o fluxo upstream de projeto recém-compartilhado rebuscado pelo saga', () => {
    const ProjectClass = makeProjectClass();

    Project.reducer(updateHandle({ isAvailable: false, projectManagers: [] }), ProjectClass);

    expect(ProjectClass.upsert).toHaveBeenCalled();
  });
});
